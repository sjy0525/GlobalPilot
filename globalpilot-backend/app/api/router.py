from fastapi import APIRouter

from app.modules.auth.router import router as auth_router
from app.modules.evaluation.router import router as evaluation_router
from app.modules.rag.router import router as rag_router
from app.modules.brands.router import router as brands_router
from app.modules.users.router import router as users_router
from app.modules.briefs.router import router as briefs_router
from app.modules.marketing.router import router as marketing_router
from app.modules.marketing.revision_router import router as revision_router
from app.modules.memory.router import router as memory_router
from app.modules.teams.router import router as teams_router
from app.modules.tickets.router import router as tickets_router

api_router = APIRouter(prefix="/api")

api_router.include_router(users_router)
api_router.include_router(auth_router)
api_router.include_router(brands_router)
api_router.include_router(rag_router)
api_router.include_router(briefs_router)
api_router.include_router(marketing_router)
api_router.include_router(revision_router)
api_router.include_router(memory_router)
api_router.include_router(evaluation_router)
api_router.include_router(teams_router)
api_router.include_router(tickets_router)
