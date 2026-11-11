"""Column docs for the genre findings store (GENRE-03, GENRE-05).

Own module for the same reason ``column_docs_lyrics`` is one: the combined
column-doc table would otherwise cross the file-size gate.
"""

from __future__ import annotations

GENRE_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "genre_findings": {
        "stable_id": ("Track the finding is about."),
        "provider": (
            "Who said it: odj-fused, jev, deepseek, or an attached provider id such "
            "as beatport. With stable_id, the primary key."
        ),
        "status": ("ok when genre is set, unknown when the provider gave nothing usable."),
        "genre": ("A genre name from the taxonomy, or NULL when unknown."),
        "confidence": ("0 to 1 as the provider reported it; NULL when it gave none."),
        "reason": ("Why the finding is unknown, or free text from an attached provider."),
        "evidence_json": (
            "JSON evidence: the search query and version, the fusion rule, or an attached finding's source."
        ),
        "model": ("Model id that produced a computed finding; NULL for attached ones."),
        "version": ("Taxonomy and prompt version a computed finding was made under."),
        "request_digest": (
            "SHA-256 of the request a computed finding answered. Same digest on re- "
            "analysis means up to date; NULL means a call failed and the track is "
            "asked again."
        ),
        "cost_usd": ("Measured cost of the calls behind this finding, in US dollars."),
        "created_at": ("UTC RFC 3339 time the finding was stored."),
    },
    "genre_vectors": {
        "stable_id": ("Track the vector is for."),
        "provider": ("Provider that produced the vector (jev). With stable_id, the primary key."),
        "taxonomy_version": ("Taxonomy the vector's keys belong to; other versions are not compared."),
        "vector_json": ("JSON object mapping every taxonomy genre to its probability."),
        "created_at": ("UTC RFC 3339 time the vector was stored."),
    },
    "genre_search_cache": {
        "query": ("Primary key: the exact search query string."),
        "query_version": ("Version of the query template; a row of another version is not reused."),
        "text": ("The search snippets as given to the models."),
        "created_at": ("UTC RFC 3339 time the search was stored."),
    },
    "genre_config": {
        "key": ("Primary key: the setting name."),
        "value_json": ("The setting's value as JSON."),
        "updated_at": ("UTC RFC 3339 time the setting was last written."),
    },
    "enrichment_claims": {
        "claim_id": ("Primary key: uuid for this versioned enrichment claim."),
        "stable_id": ("Track the claim is about."),
        "dimension": ("What the claim describes: genre, mood, bpm, key, meter, description, and similar."),
        "kind": (
            "semantic_tag, generated_description, or deterministic_fact; "
            "deterministic facts are never mixed into semantic tags."
        ),
        "source": ("Who produced the value: open-dj analysis, a vendor id, or an agent provider."),
        "envelope_json": (
            "Full claim envelope JSON: scope, provider model, schema version, value, "
            "confidence, generated time, input fingerprint, raw evidence pointer."
        ),
        "created_at": ("UTC RFC 3339 time the claim was stored."),
    },
    "genre_tag_writes": {
        "id": ("Primary key: the write id undo takes."),
        "stable_id": ("Track whose file was written."),
        "path": ("Absolute path of the audio file written."),
        "previous_genre": ("The file's genre tag before the write; NULL when it had none."),
        "written_genre": ("The genre written into the file."),
        "provider": ("Provider whose genre was chosen for the write."),
        "backup_path": ("Byte copy of the file before the write, named by its SHA-256."),
        "written_at": ("UTC RFC 3339 time of the write."),
        "undone_at": ("UTC RFC 3339 time undo restored the backup; NULL while the write stands."),
        "post_sha256": ("SHA-256 of the file as written. Undo refuses a file that no longer matches it."),
    },
}
