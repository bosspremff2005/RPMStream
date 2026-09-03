"""Progress maths + the rendered status message."""

from app.ui.animations import Stage, animate, stage_frame, stage_timeline
from app.ui.progress import UploadProgress, render_progress, render_queue_added


def make(**kwargs) -> UploadProgress:
    base = dict(job_id="abcd1234", file_name="Movie.mp4", total=10_737_418_240)
    base.update(kwargs)
    return UploadProgress(**base)


def test_fraction_percent_eta():
    progress = make(transferred=6_657_199_308, speed=15_518_925)
    assert 61 <= progress.percent <= 63
    assert progress.eta is not None and 250 < progress.eta < 270


def test_eta_is_none_without_speed():
    assert make(transferred=1024, speed=0).eta is None


def test_advance_clamps_to_total():
    progress = make(transferred=10_737_418_230)
    progress.advance(1_000_000)
    assert progress.transferred == progress.total


def test_render_progress_contains_the_live_fields():
    progress = make(transferred=6_657_199_308, speed=15_518_925, stage=Stage.TRANSFERRING)
    text = render_progress(progress, tick=1, bar_width=16)

    assert "RPMStream" in text
    assert "Movie.mp4" in text
    assert "62%" in text
    assert "6.2 GB / 10.0 GB" in text
    assert "14.8 MB/s" in text
    assert "ETA:" in text
    assert "Transferring video" in text
    assert "█████████" in text  # filled blocks


def test_render_progress_escapes_the_file_name():
    progress = make(file_name="<script>.mp4")
    assert "<script>" not in render_progress(progress)
    assert "&lt;script&gt;" in render_progress(progress)


def test_render_progress_shows_retry_state():
    progress = make(stage=Stage.PREPARING, attempt=2, max_retries=3)
    text = render_progress(progress, tick=0)
    assert "Retry 1/3" in text


def test_queue_added_screen():
    text = render_queue_added(
        job_id="abcd1234",
        file_name="Movie.mp4",
        size=9_019_431_936,
        position=2,
        queue_length=3,
    )
    assert "Added to Queue" in text
    assert "Movie.mp4" in text
    assert "8.4 GB" in text
    assert "<b>#2</b> of 3" in text


def test_stage_frame_cycles():
    frames = {stage_frame(Stage.TRANSFERRING, tick) for tick in range(12)}
    assert len(frames) > 1
    assert animate(Stage.FINALIZING, 0).startswith("✨")


def test_stage_timeline_marks_the_past_and_future():
    timeline = stage_timeline(Stage.TRANSFERRING)
    assert timeline.count("✅") == 3
    assert "⚡" in timeline
    assert "▫️" in timeline
    assert stage_timeline(Stage.COMPLETE) == ""
