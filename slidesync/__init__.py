"""slidesync — bidirectional Slidev markdown <-> Google Slides sync.

`push` builds **native** Slides objects (title/body placeholders, bullets,
tables, positioned images), so the result stays editable rather than a flat
image; `pull` reconstructs Slidev markdown from those native objects;
`roundtrip` proves the loop is stable. Auth is borrowed from the `gog` CLI.

Library usage::

    from slidesync import get_services, load_slides, push, pull_slides, write_slidev

    slides_api, drive = get_services()
    push(slides_api, drive, deck_id, load_slides(Path("deck.slidev.md")),
         anchor=None, prune=False)

CLI: ``slidesync push|pull|roundtrip|layouts|make-templates`` (see ``--help``).
"""

from slidesync._sync import (
    Para,
    Run,
    Slide,
    build_slides,
    get_services,
    load_slides,
    main,
    pull_slides,
    push,
    split_slides,
    write_slidev,
)

__version__ = "0.14.0"

__all__ = [
    "Para",
    "Run",
    "Slide",
    "__version__",
    "build_slides",
    "get_services",
    "load_slides",
    "main",
    "pull_slides",
    "push",
    "split_slides",
    "write_slidev",
]
