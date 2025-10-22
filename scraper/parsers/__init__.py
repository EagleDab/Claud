"""Collection of scraper adapters."""
from .base import BaseParser, ProductSnapshot, ScraperError
from .mk4s import MK4SParser
from .petrovich import PetrovichParser
from .whitehills import WhiteHillsParser

ADAPTER_REGISTRY = {
    "petrovich": PetrovichParser,
    "whitehills": WhiteHillsParser,
    "mk4s": MK4SParser,
}

__all__ = [
    "BaseParser",
    "ProductSnapshot",
    "ScraperError",
    "PetrovichParser",
    "WhiteHillsParser",
    "MK4SParser",
    "ADAPTER_REGISTRY",
]
