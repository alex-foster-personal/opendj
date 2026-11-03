"""Operator CLI for the AI gateway Worker's install tokens (apps/ai_gateway_worker).

Talks to the Worker's ``/admin`` endpoints with the ``ADMIN_TOKEN`` secret:

    python -m apps.ai_gateway_admin mint [--plan free] [--label NAME]
    python -m apps.ai_gateway_admin revoke INSTALL_ID
    python -m apps.ai_gateway_admin set-plan INSTALL_ID PLAN
    python -m apps.ai_gateway_admin usage INSTALL_ID [--days 7]

The minted ``token`` is printed once and is what an engine sets as
``MDT_ASSISTANT_INSTALL_TOKEN``; the Worker keeps only its SHA-256.
"""
