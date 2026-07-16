from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "046_relationship_graph_lookup_indexes.py"


def test_relationship_graph_lookup_indexes_cover_both_seed_directions() -> None:
    source = MIGRATION.read_text()

    assert 'revision: str = "046"' in source
    assert 'down_revision: Union[str, None] = "045_embeddings_item_chunk_index"' in source
    assert "ix_item_relationships_source_confidence_target" in source
    assert "ix_item_relationships_target_confidence_source" in source
    assert "confidence DESC" in source
    assert source.count("CREATE INDEX CONCURRENTLY") == 1
    assert source.count("DROP INDEX CONCURRENTLY") == 2
    assert source.count("autocommit_block") == 2
    assert "indisvalid" in source
    assert "indisready" in source
