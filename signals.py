"""In-process Django signals mirroring the comm emits.

The canonical, cross-service resource hook is the
``calendar.occurrence.materialized`` comm emit (schemas/emits/) — a monolith
app-layer may prefer a synchronous Django signal for the same moment (e.g.
legacy creating a Room). Both fire from :func:`services.materialize`.
"""
import django.dispatch

# Sent when a recurring occurrence is first persisted (gains own state).
# kwargs: occurrence (Event), series (Event).
occurrence_materialized = django.dispatch.Signal()
