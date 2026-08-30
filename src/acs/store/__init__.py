"""Persistence layer."""

from __future__ import annotations

from acs.config import Settings
from acs.store.base import Store
from acs.store.memory import MemoryStore

__all__ = ["MemoryStore", "Store", "build_store"]


def build_store(settings: Settings) -> Store:
    """Return the configured store backend."""
    if settings.store_backend == "dynamodb":
        from acs.store.dynamodb import DynamoDbStore

        return DynamoDbStore(
            table_name=settings.table_name,
            region_name=settings.aws_region,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
    return MemoryStore()
