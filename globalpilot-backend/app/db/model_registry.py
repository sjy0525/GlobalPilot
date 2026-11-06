"""Load application ORM models before schema initialization."""

from app.modules.brands.models import Brand  # noqa: F401
from app.modules.briefs.models import Brief  # noqa: F401
from app.modules.rag.models import KnowledgeAsset, KnowledgeDocument  # noqa: F401
from app.modules.evaluation.models import EvalCase, EvalExecutorLease, EvalRun, EvalTask, EvalTestSet  # noqa: F401
from app.modules.users.models import User  # noqa: F401
from app.modules.teams.models import Team
from app.modules.memory.models import MemoryJob  # noqa: F401
from app.modules.marketing.models import (  # noqa: F401
    RevisionDraft, RevisionOperation, RevisionSample, RevisionTask,
)
