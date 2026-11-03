"""The INVENTORY of features an entitlement can be asked about.

openDJ has no paid features yet, so nothing here is sold today.  The catalog
exists so that each gateable feature is added HERE, in one reviewable place,
rather than as an ``if`` somewhere in a route.  The first entry,
``ai.assistant``, is enforced by the gateway Worker itself; billing that moves
an install between its plans is the BILL-02 seam, not wired.

``server_side`` is not decoration.  ``(private path)`` BILL-01 makes it the
admission test for the catalog: a feature is only sellable if it needs a
server we run, because then the server refusing work IS the enforcement.  A
purely local feature cannot be enforced against a user holding the source, so
adding one here with ``server_side=False`` would be adding a gate that is a
one-line patch away from being off -- ``assert_sellable`` refuses it.

Note the resolver does NOT consult this catalog to answer :func:`has`.  An
unknown feature id is entitled while no provider is configured (ENT-04), so a
missing catalog entry can never quietly deny a user something.  The catalog is
what the HTTP surface ENUMERATES, so an agent can read the gated set without
reading the source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    """One thing an account can be entitled to.

    ``feature_id`` is the stable wire identifier, lowercase dotted, and it is
    what every :func:`apps.entitlements.has` call site passes.  It must never
    be a plan name: a feature outlives the plan it happens to be sold in.
    """

    feature_id: str
    label: str
    #: True when the feature needs a server we run, so the entitlement
    #: enforces itself.  See the module docstring: this is an admission test.
    server_side: bool
    note: str


#: Every gateable feature.  See the module docstring before adding one.
FEATURES: tuple[Feature, ...] = (
    Feature(
        feature_id="ai.assistant",
        label="AI assistant via the Open DJ gateway",
        server_side=True,
        note=(
            "Model calls through the hosted gateway Worker (apps/ai_gateway_worker), "
            "which holds the provider key and refuses work per install token: plan "
            "model allow-list, output-token cap and daily spend cap. Bringing your "
            "own OPENROUTER_API_KEY bypasses the gateway and is not gated."
        ),
    ),
)

FEATURES_BY_ID: dict[str, Feature] = {
    feature.feature_id: feature for feature in FEATURES
}


def assert_sellable(feature: Feature) -> None:
    """Refuse a catalog entry that cannot actually be enforced.

    Called by the catalog's own test over every entry, so a client-only
    "paid" feature cannot be added without the argument being had first.
    """
    if not feature.server_side:
        raise ValueError(
            f"feature {feature.feature_id!r} is marked server_side=False, so "
            "the only thing standing between a user and it is a boolean in "
            "source they already have. Per (private path) BILL-01 a paid "
            "feature MUST need a server we run, so that the server refusing "
            "work is the enforcement. Either move the enforced part "
            "server-side or do not sell it."
        )


__all__ = ["FEATURES", "FEATURES_BY_ID", "Feature", "assert_sellable"]
