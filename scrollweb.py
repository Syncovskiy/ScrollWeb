from __future__ import annotations

import argparse

from scroll_web_video import CaptureSettings, render_scroll_video

DEFAULT_SETTINGS = CaptureSettings()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a 30 FPS MP4 with a smooth scroll of a web page.",
    )
    parser.add_argument("url", nargs="?", help="Web page URL. Omit to open the GUI.")
    parser.add_argument("-o", "--output", default="scroll.mp4", help="Output MP4 path.")
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_SETTINGS.width,
        help="Viewport width.",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_SETTINGS.height,
        help="Viewport height.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=DEFAULT_SETTINGS.fps,
        help="Video FPS.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=900.0,
        help="Average scroll speed in CSS pixels per second.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Scroll duration in seconds. Overrides --speed.",
    )
    parser.add_argument(
        "--wait-after-load",
        type=float,
        default=DEFAULT_SETTINGS.wait_after_load,
        help="Seconds to wait after the page finishes loading before recording.",
    )
    parser.add_argument(
        "--easing",
        choices=("smoothstep", "linear"),
        default="smoothstep",
        help="Scroll motion curve.",
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help="Skip the pre-scroll pass used to load lazy content.",
    )
    parser.add_argument("--crf", type=int, default=18, help="H.264 quality, 0-51.")
    parser.add_argument(
        "--no-sync-animations",
        action="store_true",
        help="Do not synchronize CSS/JS animation time to the output video FPS.",
    )
    return parser


async def run_cli(args: argparse.Namespace) -> None:
    settings = CaptureSettings(
        width=args.width,
        height=args.height,
        fps=args.fps,
        speed=args.speed,
        duration=args.duration,
        wait_after_load=args.wait_after_load,
        preload=not args.no_preload,
        easing=args.easing,
        crf=args.crf,
        sync_animations=not args.no_sync_animations,
    )

    last_percent = -1

    def progress(message: str, value: float | None) -> None:
        nonlocal last_percent
        if value is None:
            print(message, flush=True)
        else:
            percent = int(value * 100)
            if percent != last_percent:
                last_percent = percent
                print(f"{message}: {percent}%", flush=True)

    output = await render_scroll_video(args.url, args.output, settings, progress)
    print(f"Saved: {output}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.url:
        from scroll_web_video.gui import main as gui_main

        gui_main()
        return

    import asyncio

    asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
