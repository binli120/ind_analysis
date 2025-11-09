"""IND pipeline package that wires stage modules into the central registry."""

# ind_pipeline package initialiser.
#
# Importing modules here ensures they self-register with the central registry.

from .registry import MODULE_REGISTRY

# Core modules
from . import pdf_extraction  # noqa: F401
from . import zeroshot_labeling  # noqa: F401

# Pipeline stage placeholders
from . import upload_ingestion  # noqa: F401
from . import pdf_parsing_chunking  # noqa: F401
from . import classification_template_matching  # noqa: F401
from . import metadata_summary_extraction  # noqa: F401
from . import embedding_indexing  # noqa: F401
from . import reranker  # noqa: F401
from . import module26_narrative_writers  # noqa: F401
from . import module26_tabulators  # noqa: F401
from . import module24_synthesizer  # noqa: F401
from . import validation_scoring  # noqa: F401
from . import packaging_submission  # noqa: F401

from .event_sequence import STAGE_SEQUENCE, STAGE_SUCCESSORS
__all__ = ["MODULE_REGISTRY", "STAGE_SEQUENCE", "STAGE_SUCCESSORS"]
