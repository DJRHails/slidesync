"""Template-slot validation: content a template silently drops fails the push.

`validate_slots` flags authored content the render has no slot for — a heading
or prose caption on a text-free `graph`/`full` template, an `# h1` on an
`equation` slide, an image on a `prompt`/`code`/`equation` slide — so the push
stops up front instead of publishing a deck missing what the author wrote.
Link-only paragraphs on text-free templates (the crop → full-figure trace-link
convention) and comments are exempt.
"""

from slidesync._sync import build_slides, split_slides, validate_slots


def _slides(body, template="graph"):
    md = f"""---
theme: seriph
---

---
template: {template}
id: fig
---
{body}
"""
    return build_slides(split_slides(md))


def test_graph_with_heading_is_flagged():
    problems = validate_slots(_slides("# A headline\n\n![f](fig.png)"))
    assert len(problems) == 1
    assert "heading" in problems[0] and "'fig'" in problems[0]


def test_graph_with_prose_caption_is_flagged():
    problems = validate_slots(_slides(
        "![f](fig.png)\n\nA prose caption that never renders."))
    assert len(problems) == 1
    assert "body text" in problems[0]


def test_graph_with_link_only_line_is_clean():
    body = ("![f](fig.png)\n\n"
            "[each harm asked on its own →](https://x/a) · [all in one →](https://x/b)")
    assert validate_slots(_slides(body)) == []


def test_graph_with_comment_is_clean():
    assert validate_slots(_slides("![f](fig.png)\n\n<!-- a speaker note -->")) == []


def test_graph_with_table_is_flagged():
    problems = validate_slots(_slides("![f](fig.png)\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
    assert problems and "table" in problems[0]


def test_equation_with_h1_is_flagged():
    problems = validate_slots(_slides(
        "# Dropped headline\n## THE KICKER\n\n$$x^2$$", template="equation"))
    assert len(problems) == 1
    assert "kicker" in problems[0]


def test_equation_with_lone_kicker_is_clean():
    assert validate_slots(_slides("## THE KICKER\n\n$$x^2$$",
                                  template="equation")) == []


def test_prompt_with_image_is_flagged():
    problems = validate_slots(_slides(
        "## TITLE\n\n![f](fig.png)\n\n```\nverbatim\n```", template="prompt"))
    assert problems and "image" in problems[0]


def test_supported_templates_are_untouched():
    md = """---
theme: seriph
---

---
template: topic
id: a
---
# Headline
## KICKER

Body text is a real slot here.

---
template: content
id: b
---
## Title

- a bullet
"""
    assert validate_slots(build_slides(split_slides(md))) == []
