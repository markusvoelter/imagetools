"""The Ken Burns title overlay must size the subtitle as a fixed fraction of
the title, not fit it to its own width (which made short subtitles appear
larger than the title)."""

from image_tools import ken_burns as kb


def test_subtitle_font_is_75pct_of_title(monkeypatch):
    # Pin the title's fitted size so the subtitle's derived size is known.
    monkeypatch.setattr(kb, "_find_font_size_for_width", lambda *a, **k: 100)

    sizes = []
    real_load = kb._load_font

    def recording_load(candidates, size):
        sizes.append(size)
        return real_load(candidates, size)

    monkeypatch.setattr(kb, "_load_font", recording_load)

    kb._build_title_overlay("A Long Title", "Sub", 1920, 1080)

    # Title is loaded at the pinned 100; subtitle at 75% of it.
    assert sizes == [100, 75]


def test_subtitle_never_larger_than_title(monkeypatch):
    monkeypatch.setattr(kb, "_find_font_size_for_width", lambda *a, **k: 40)
    sizes = []
    real_load = kb._load_font
    monkeypatch.setattr(
        kb, "_load_font",
        lambda c, s: (sizes.append(s), real_load(c, s))[1])

    # Even a one-character subtitle stays smaller than the title.
    kb._build_title_overlay("Title", "x", 1080, 1920)

    title_size, sub_size = sizes
    assert sub_size < title_size
    assert sub_size == round(title_size * kb.KB_SUBTITLE_SIZE_FRAC)
