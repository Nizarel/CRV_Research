"""CRV research ingestion pipeline: durable, evidence-first.

Layering (enforced by import-linter contracts in ``pyproject.toml``):

``domain``   pure rules and models, no I/O
``ports``    Protocols the rest of the pipeline depends on
``adapters`` Azure, PostgreSQL and in-memory implementations of the ports
``stages``   business steps, depending only on ports and domain
``runtime``  coordinator, worker and wiring
``cli``      ``crv-pipeline`` and compatibility ``tizagent-pipeline`` entry points
``tools``    standalone discovery and extraction-policy bakeoff tools
"""
