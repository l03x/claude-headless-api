from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.api_docs import SPEC_YAML, agent_guide, breakable, load_spec, markdown, page_model
from app.routers.admin import templates

router = APIRouter(include_in_schema=False)
templates.env.filters["md"] = markdown
templates.env.filters["breakable"] = breakable


@router.get("/docs")
def api_docs(request: Request):
    # Public, like the spec itself: API users have tokens, not dashboard accounts.
    return templates.TemplateResponse(request, "api_docs.html", page_model(str(request.base_url)))


@router.get("/openapi.json")
def openapi_json():
    return JSONResponse(load_spec())


@router.get("/openapi.yaml")
def openapi_yaml():
    return FileResponse(SPEC_YAML, media_type="application/yaml")


@router.get("/llms.txt")
def llms_txt(request: Request):
    # The llms.txt convention: a Markdown brief that agents fetch before using a site's API.
    return PlainTextResponse(agent_guide(str(request.base_url)), media_type="text/markdown; charset=utf-8")
