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


def get_parser(file_type: str) -> BaseParser:
    if file_type not in _PARSERS:
        raise NoParserError(f"no parser for file type '{file_type}'")
    return _PARSERS[file_type]()


def supported_types() -> set[str]:
    return set(_PARSERS)


def missing_types() -> set[str]:
    """File types the gate allows but no parser handles yet."""
    from ingestion.safety import FILE_TYPES
    return set(FILE_TYPES.values()) - supported_types()
