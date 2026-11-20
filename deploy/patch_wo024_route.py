"""Apply only the WO-024 manual Shopping route to the pinned active image."""

import ast
from pathlib import Path

ROUTES = Path("/app/app/api/routes.py")

replacement = '''@router.post(
    "/shopping",
    response_model=ShoppingItem,
    status_code=status.HTTP_201_CREATED,
    tags=["shopping"],
    responses={
        400: {"description": "X-Household-Id is required"},
        401: {"description": "An authenticated install is required"},
        403: {"description": "The install is not a member of this household"},
        422: {"description": "The Shopping request is invalid"},
        409: {"description": "request_id was previously used with different values"},
        503: {"description": "Shopping writes require Cosmos; nothing was saved"},
    },
)
async def add_shopping_intent(
    request: AddShoppingIntentRequest,
    response: Response,
    context: Annotated[AuthorizedHousehold, Depends(get_authorized_household)],
    service: Annotated[ShoppingIntentService | None, Depends(get_shopping_intent_service)],
) -> ShoppingItem:
    """Persist one pending Shopping item after the App's explicit Add action."""
    if service is None:
        raise HTTPException(503, "Shopping writes need Cosmos; no changes were saved.")
    try:
        result = await service.add(
            AddShoppingIntent(
                household_id=context.household_id,
                request_id=request.request_id,
                name=request.name,
                quantity=request.quantity,
                unit=request.unit,
            )
        )
    except ShoppingIntentError as error:
        raise HTTPException(422, str(error)) from error
    except OperationIdConflict as error:
        raise HTTPException(409, "request_id was already used with different values.") from error
    if result.status is WriteStatus.ALREADY_APPLIED:
        response.status_code = status.HTTP_200_OK
    return result.item'''

needed_imports = {
    "app.core.dependencies": {"get_shopping_intent_service"},
    "app.services.cosmos_write": {"OperationIdConflict", "WriteStatus"},
    "app.services.shopping.intents": {
        "AddShoppingIntent",
        "ShoppingIntentError",
        "ShoppingIntentService",
    },
}

source = ROUTES.read_text(encoding="utf-8")
tree = ast.parse(source)
matches = []
for node in tree.body:
    if not isinstance(node, ast.AsyncFunctionDef) or node.name != "add_shopping_intent":
        continue
    if any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "router"
        and decorator.func.attr == "post"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == "/shopping"
        for decorator in node.decorator_list
    ):
        matches.append(node)

if len(matches) != 1:
    raise SystemExit("Expected exactly one POST /shopping handler in the pinned base image.")

handler = matches[0]
lines = source.splitlines()
start = min(decorator.lineno for decorator in handler.decorator_list) - 1
end = handler.end_lineno
old_block = "\n".join(lines[start:end])
if (
    "HTTP_501_NOT_IMPLEMENTED" not in old_block
    or "no changes were saved" not in old_block.lower()
):
    raise SystemExit("Pinned base POST /shopping is not the expected fail-closed handler.")

lines[start:end] = replacement.splitlines()
tree = ast.parse("\n".join(lines))
existing_imports: dict[str, set[str]] = {}
last_import_line = 0
for node in tree.body:
    if isinstance(node, ast.ImportFrom) and node.module:
        existing_imports.setdefault(node.module, set()).update(
            alias.name for alias in node.names
        )
        last_import_line = max(last_import_line, node.end_lineno)

imports_to_add = []
for module, names in needed_imports.items():
    missing = sorted(names - existing_imports.get(module, set()))
    if missing:
        imports_to_add.append(f"from {module} import {', '.join(missing)}")

lines[last_import_line:last_import_line] = imports_to_add
patched = "\n".join(lines) + "\n"
ast.parse(patched)
ROUTES.write_text(patched, encoding="utf-8")
print("WO-024 patched only POST /shopping and its required imports.")
