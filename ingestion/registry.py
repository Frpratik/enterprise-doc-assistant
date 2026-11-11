"""
Parser registry = the routing desk.

file type -> parser. Adding support for a new file type means writing ONE new
parser class with @register. Nothing else in the pipeline changes.
"""
from ingestion.parsers.base import BaseParser

_PARSERS: dict[str, type[BaseParser]] = {}


class NoParserError(Exception):
    pass


def register(parser_cls: type[BaseParser]) -> type[BaseParser]:
    """Class decorator: @register puts the parser in the routing table."""
    for ft in parser_cls.file_types:
        if ft in _PARSERS and _PARSERS[ft] is not parser_cls:
            raise ValueError(f"two parsers registered for '{ft}': "
                             f"{_PARSERS[ft].__name__} and {parser_cls.__name__}")
        _PARSERS[ft] = parser_cls
    return parser_cls


# parser modules register themselves when imported
PARSER_MODULES = ["text", "sheet", "docx", "pptx", "image", "pdf"]
_loaded = False


def load_all() -> None:
    """Import every parser module once, so their @register runs."""
    global _loaded
    if _loaded:
        return
    import importlib
    for mod in PARSER_MODULES:
        try:
            importlib.import_module(f"ingestion.parsers.{mod}")
        except ModuleNotFoundError as e:
            if e.name != f"ingestion.parsers.{mod}":
                raise                       # a real missing package, don't hide it
    _loaded = True


def get_parser(file_type: str) -> BaseParser:
    load_all()
    if file_type not in _PARSERS:
        raise NoParserError(f"no parser for file type '{file_type}'")
    return _PARSERS[file_type]()


def supported_types() -> set[str]:
    load_all()
    return set(_PARSERS)


def missing_types() -> set[str]:
    """File types the gate allows but no parser handles yet."""
    from ingestion.safety import FILE_TYPES
    return set(FILE_TYPES.values()) - supported_types()
