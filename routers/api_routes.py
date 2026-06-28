from fastapi import APIRouter
from . import system_routes
from . import account_routes
from . import service_routes
from . import sms_routes
from . import fork_legacy_routes
from utils.auth_core import router as email_router
from utils.auth_core import code_pool, cache_lock, generate_payload

router = APIRouter()

router.include_router(system_routes.router)
router.include_router(account_routes.router)
router.include_router(service_routes.router)
router.include_router(sms_routes.router)
router.include_router(email_router)
# Fork-only compatibility endpoints (proxy controls, Neuralwatt, Codex2API) are kept last
# so upstream v17 split routes win when paths overlap.
router.include_router(fork_legacy_routes.router)
