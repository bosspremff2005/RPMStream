"""Formatting helpers."""

from app.utils.fmt import (
    ellipsis,
    format_duration,
    format_eta,
    html_escape,
    human_size,
    human_speed,
    progress_bar,
    safe_filename,
)


def test_human_size_scales_units():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(6_764_576_358) == "6.3 GB"
    assert human_size(10_737_418_240) == "10.0 GB"


def test_human_size_handles_garbage():
    assert human_size(None) == "—"
    assert human_size(-5) == "0 B"


def test_human_speed():
    assert human_speed(0) == "0 KB/s"
    assert human_speed(15_518_925) == "14.8 MB/s"


def test_eta_formatting():
    assert format_eta(None) == "--:--"
    assert format_eta(-1) == "--:--"
    assert format_eta(252) == "04:12"
    assert format_eta(3723) == "1:02:03"
    assert format_duration(0) == "00:00"


def test_progress_bar_is_stable_and_bounded():
    assert progress_bar(0.0, 16) == "░" * 16
    assert progress_bar(1.0, 16) == "█" * 16
    bar = progress_bar(0.62, 16)
    assert len(bar) == 16
    assert bar.startswith("█████████")
    # monotonic: a bigger fraction never produces a shorter filled area
    filled = lambda value: sum(1 for char in progress_bar(value, 16) if char != "░")  # noqa: E731
    assert filled(0.2) <= filled(0.5) <= filled(0.9)


def test_progress_bar_clamps_out_of_range():
    assert len(progress_bar(5.0, 10)) == 10
    assert len(progress_bar(-3.0, 10)) == 10


def test_safe_filename_strips_dangerous_characters():
    assert safe_filename('bad"na\\me*.mp4') == "badname.mp4"
    assert safe_filename("  spaced   out.mkv ") == "spaced out.mkv"
    assert safe_filename("") == "video.mp4"
    assert safe_filename(None) == "video.mp4"
    long_name = safe_filename("x" * 400 + ".mp4")
    assert long_name.endswith(".mp4")
    assert len(long_name) <= 120


def test_safe_filename_keeps_unicode():
    assert safe_filename("नमस्ते movie 1080p.mkv") == "नमस्ते movie 1080p.mkv"


def test_ellipsis_keeps_the_tail():
    assert ellipsis("short.mp4") == "short.mp4"
    text = ellipsis("a" * 100 + "-FINALE.mkv", 30)
    assert len(text) <= 30
    assert text.endswith("-FINALE.mkv")


def test_html_escape():
    assert html_escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert html_escape(None) == ""
