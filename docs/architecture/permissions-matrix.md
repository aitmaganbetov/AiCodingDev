# permissions-matrix (ТЗ §21)

Нормативная матрица прав — [`docs/api/API_DESIGN.md §5`](../api/API_DESIGN.md) (каталог прав, «эндпоинт × роль», approvals, директивы).
Реализация как код — `packages/identity/aicd_identity/rbac.py`; соответствие проверяют:
- `tests/unit/test_rbac.py` — каждая ячейка матрицы §5.1 (145 проверок), правила approvals, директив и переходов находок (§5.5);
- `tests/contract/test_contracts.py` — `x-required-permission` каждой реализованной операции совпадает с контрактом;
- `tests/security/test_cross_tenant_api.py`, `tests/integration/test_api_*.py` — проверка на уровне HTTP.
