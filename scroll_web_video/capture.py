from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

ProgressCallback = Callable[[str, float | None], None]


@dataclass(slots=True)
class CaptureSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    speed: float = 150.0
    duration: float | None = None
    hold_start: float = 0.35
    hold_end: float = 0.65
    wait_after_load: float = 1.0
    timeout_ms: int = 45_000
    preload: bool = True
    easing: str = "smoothstep"
    crf: int = 18
    ffmpeg_preset: str = "medium"
    frame_settle_ms: int = 12
    headless: bool = True
    sync_animations: bool = True


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        raise ValueError("URL is empty.")

    local_file = Path(value).expanduser()
    if local_file.exists() and local_file.is_file():
        return local_file.resolve().as_uri()

    parsed = urlparse(value)
    if parsed.scheme:
        return value

    return f"https://{value}"


async def render_scroll_video(
    url: str,
    output: str | Path,
    settings: CaptureSettings | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    settings = _validated_settings(settings or CaptureSettings())
    target_url = normalize_url(url)
    output_path = Path(output).expanduser().resolve()
    if output_path.suffix.lower() != ".mp4":
        output_path = output_path.with_suffix(".mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    _notify(progress, "Opening page", 0.0)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        context = await browser.new_context(
            viewport={"width": settings.width, "height": settings.height},
            device_scale_factor=1,
            ignore_https_errors=True,
        )
        page = await context.new_page()
        if settings.sync_animations:
            await page.clock.install()

        try:
            await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=settings.timeout_ms,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                pass

            if settings.wait_after_load > 0:
                await page.wait_for_timeout(int(settings.wait_after_load * 1000))

            _notify(progress, "Measuring page", 0.04)
            metrics = await _page_metrics(page)
            scroll_distance = max(0.0, metrics["height"] - settings.height)

            if settings.preload and scroll_distance > 0:
                await _preload_page(page, scroll_distance, settings.height, progress)
                metrics = await _page_metrics(page)
                scroll_distance = max(0.0, metrics["height"] - settings.height)

            timing = _build_timing(settings, scroll_distance)
            if settings.sync_animations:
                await _pause_page_clock(page)

            _notify(progress, f"Rendering {timing.total_frames} frames", 0.08)

            process = _start_ffmpeg(output_path, settings)
            previous_clock_ms = 0
            try:
                for frame_index in range(timing.total_frames):
                    frame_time_ms = round(frame_index * 1000 / settings.fps)
                    scroll_y = _scroll_position_for_frame(
                        frame_index,
                        timing.total_frames,
                        scroll_distance,
                        settings,
                        timing.scroll_duration,
                    )
                    await page.evaluate("(y) => window.scrollTo(0, y)", scroll_y)
                    if settings.sync_animations:
                        previous_clock_ms = await _advance_video_clock(
                            page,
                            frame_index,
                            settings.fps,
                            previous_clock_ms,
                        )
                        timeline_state = await _sync_frame_timeline(
                            page,
                            frame_time_ms,
                        )
                        if settings.frame_settle_ms > 0:
                            settle_ms = settings.frame_settle_ms
                            if timeline_state["media"] > 0:
                                settle_ms = max(settle_ms, 40)
                            await page.wait_for_timeout(settle_ms)
                    elif settings.frame_settle_ms > 0:
                        await page.wait_for_timeout(settings.frame_settle_ms)

                    png = await page.screenshot(
                        type="png",
                        full_page=False,
                        scale="css",
                        animations="allow",
                    )
                    frame = Image.open(BytesIO(png)).convert("RGB")
                    if frame.size != (settings.width, settings.height):
                        frame = frame.resize((settings.width, settings.height))

                    process.stdin.write(frame.tobytes())
                    render_progress = 0.08 + 0.90 * (
                        (frame_index + 1) / timing.total_frames
                    )
                    _notify(progress, "Rendering frames", render_progress)
            finally:
                if process.stdin:
                    process.stdin.close()

            stderr = process.stderr.read().decode("utf-8", errors="replace")
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"ffmpeg failed with code {return_code}: {stderr}")

            _notify(progress, "Done", 1.0)
            return output_path
        finally:
            await context.close()
            await browser.close()


@dataclass(slots=True)
class _Timing:
    scroll_duration: float
    total_frames: int


def _validated_settings(settings: CaptureSettings) -> CaptureSettings:
    width = _even_int(settings.width, minimum=320)
    height = _even_int(settings.height, minimum=240)
    fps = int(settings.fps)
    if fps < 1 or fps > 120:
        raise ValueError("FPS must be between 1 and 120.")

    if settings.duration is not None and settings.duration <= 0:
        raise ValueError("Duration must be greater than zero.")

    if settings.speed <= 0:
        raise ValueError("Speed must be greater than zero.")

    if settings.easing not in {"smoothstep", "linear"}:
        raise ValueError("Easing must be 'smoothstep' or 'linear'.")

    if settings.crf < 0 or settings.crf > 51:
        raise ValueError("CRF must be between 0 and 51.")

    return CaptureSettings(
        width=width,
        height=height,
        fps=fps,
        speed=float(settings.speed),
        duration=settings.duration,
        hold_start=max(0.0, settings.hold_start),
        hold_end=max(0.0, settings.hold_end),
        wait_after_load=max(0.0, settings.wait_after_load),
        timeout_ms=max(1000, int(settings.timeout_ms)),
        preload=settings.preload,
        easing=settings.easing,
        crf=int(settings.crf),
        ffmpeg_preset=settings.ffmpeg_preset,
        frame_settle_ms=max(0, int(settings.frame_settle_ms)),
        headless=settings.headless,
        sync_animations=settings.sync_animations,
    )


def _even_int(value: int, minimum: int) -> int:
    result = max(minimum, int(value))
    if result % 2:
        result -= 1
    return result


async def _page_metrics(page) -> dict[str, float]:
    return await page.evaluate(
        """() => {
            const doc = document.documentElement;
            const body = document.body || doc;
            const height = Math.max(
                body.scrollHeight, body.offsetHeight,
                doc.clientHeight, doc.scrollHeight, doc.offsetHeight
            );
            const width = Math.max(
                body.scrollWidth, body.offsetWidth,
                doc.clientWidth, doc.scrollWidth, doc.offsetWidth
            );
            return {
                width,
                height,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight
            };
        }"""
    )


async def _preload_page(
    page,
    scroll_distance: float,
    viewport_height: int,
    progress: ProgressCallback | None,
) -> None:
    steps = min(40, max(1, math.ceil(scroll_distance / max(1, viewport_height))))
    for step in range(steps + 1):
        y = scroll_distance * (step / steps)
        await page.evaluate("(value) => window.scrollTo(0, value)", y)
        await page.wait_for_timeout(90)
        _notify(progress, "Preloading page", 0.04 + 0.04 * (step / steps))

    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(200)


def _build_timing(settings: CaptureSettings, scroll_distance: float) -> _Timing:
    if settings.duration is not None:
        scroll_duration = settings.duration
    elif scroll_distance <= 0:
        scroll_duration = 1.0
    else:
        scroll_duration = max(1.0, scroll_distance / settings.speed)

    total_seconds = scroll_duration + settings.hold_start + settings.hold_end
    total_frames = max(1, math.ceil(total_seconds * settings.fps))
    return _Timing(scroll_duration=scroll_duration, total_frames=total_frames)


def _scroll_position_for_frame(
    frame_index: int,
    total_frames: int,
    scroll_distance: float,
    settings: CaptureSettings,
    scroll_duration: float,
) -> float:
    elapsed = frame_index / settings.fps
    if elapsed <= settings.hold_start:
        return 0.0

    scroll_elapsed = elapsed - settings.hold_start
    if scroll_elapsed >= scroll_duration or total_frames <= 1:
        return scroll_distance

    ratio = max(0.0, min(1.0, scroll_elapsed / scroll_duration))
    if settings.easing == "smoothstep":
        ratio = ratio * ratio * (3.0 - 2.0 * ratio)

    return scroll_distance * ratio


async def _pause_page_clock(page) -> None:
    for margin_ms in (100, 250, 500, 1000, 2000):
        current_ms = int(await page.evaluate("() => Date.now()"))
        try:
            await page.clock.pause_at((current_ms + margin_ms) / 1000.0)
            return
        except PlaywrightError as exc:
            if "Cannot fast-forward to the past" not in str(exc):
                raise

    current_ms = int(await page.evaluate("() => Date.now()"))
    await page.clock.pause_at((current_ms + 5000) / 1000.0)


async def _advance_video_clock(
    page,
    frame_index: int,
    fps: int,
    previous_clock_ms: int,
) -> int:
    target_clock_ms = round(frame_index * 1000 / fps)
    delta_ms = target_clock_ms - previous_clock_ms
    if delta_ms > 0:
        await page.clock.run_for(delta_ms)
    return target_clock_ms


_FRAME_TIMELINE_SCRIPT = """(elapsedMs) => {
    const state = window.__scrollWebVideoSyncState || (
        window.__scrollWebVideoSyncState = {
            animations: new WeakMap(),
            media: new WeakMap()
        }
    );
    const elapsedSeconds = elapsedMs / 1000;
    const result = { animations: 0, media: 0 };

    if (typeof document.getAnimations === "function") {
        for (const animation of document.getAnimations({ subtree: true })) {
            try {
                if (!state.animations.has(animation)) {
                    const current = Number.isFinite(animation.currentTime)
                        ? animation.currentTime
                        : 0;
                    state.animations.set(animation, current - elapsedMs);
                }

                const offset = state.animations.get(animation) || 0;
                animation.pause();
                animation.currentTime = Math.max(0, offset + elapsedMs);
                result.animations += 1;
            } catch {
                // Ignore animations that the browser refuses to control.
            }
        }
    }

    for (const media of document.querySelectorAll("video,audio")) {
        try {
            if (!state.media.has(media)) {
                const current = Number.isFinite(media.currentTime)
                    ? media.currentTime
                    : 0;
                state.media.set(media, {
                    offset: current - elapsedSeconds
                });
            }

            const mediaState = state.media.get(media);
            let target = (mediaState.offset || 0) + elapsedSeconds;
            const duration = media.duration;

            if (Number.isFinite(duration) && duration > 0) {
                if (media.loop) {
                    target = ((target % duration) + duration) % duration;
                } else {
                    target = Math.max(0, Math.min(duration, target));
                }
            } else {
                target = Math.max(0, target);
            }

            media.pause();
            media.autoplay = false;
            media.playbackRate = 1;

            if (
                Number.isFinite(target)
                && Number.isFinite(media.currentTime)
                && Math.abs(media.currentTime - target) > 0.015
            ) {
                media.currentTime = target;
            }

            result.media += 1;
        } catch {
            // Some protected or live media streams cannot be seeked.
        }
    }

    return result;
}"""


async def _sync_frame_timeline(page, elapsed_ms: int) -> dict[str, int]:
    totals = {"animations": 0, "media": 0}
    for frame in list(page.frames):
        try:
            result = await frame.evaluate(_FRAME_TIMELINE_SCRIPT, elapsed_ms)
        except PlaywrightError:
            continue

        if isinstance(result, dict):
            totals["animations"] += int(result.get("animations") or 0)
            totals["media"] += int(result.get("media") or 0)

    return totals


def _start_ffmpeg(output_path: Path, settings: CaptureSettings) -> subprocess.Popen:
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg was not found. Install ffmpeg or run "
            "'python -m pip install -r requirements.txt'."
        )

    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{settings.width}x{settings.height}",
        "-r",
        str(settings.fps),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        settings.ffmpeg_preset,
        "-crf",
        str(settings.crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _ffmpeg_executable() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    try:
        import imageio_ffmpeg
    except ImportError:
        return None

    return imageio_ffmpeg.get_ffmpeg_exe()


def _notify(progress: ProgressCallback | None, message: str, value: float | None) -> None:
    if progress:
        progress(message, value)


def render_scroll_video_sync(
    url: str,
    output: str | Path,
    settings: CaptureSettings | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    return asyncio.run(render_scroll_video(url, output, settings, progress))
