"""Thin FastAPI adapter for the existing public snapshot query service."""

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from .http_contract import HttpLimits, HttpReadService
from .settings import Settings
from .operational_log import (OperationalLog, RequestLogMiddleware, add_log_arguments,
                              error_kind, from_arguments, server_logging)


def _origins(values):
    result = []
    for value in values:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("CORS origins must be explicit HTTP(S) origins")
        parsed = urlsplit(value)
        try:
            valid = parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
        except ValueError:
            valid = False
        if not valid or "*" in parsed.netloc or parsed.username or parsed.password or value != parsed.scheme + "://" + parsed.netloc:
            raise ValueError("CORS origins must be explicit HTTP(S) origins without paths or credentials")
        if value not in result:
            result.append(value)
    return result


def create_app(settings: Settings, limits: HttpLimits | None = None, *, allowed_origins=(), event_log=None):
    """Import the optional HTTP dependencies only when starting the adapter."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from starlette.exceptions import HTTPException
        from starlette.middleware.cors import CORSMiddleware
        from starlette.responses import Response
    except ImportError as error:
        raise RuntimeError("The HTTP adapter requires FastAPI and Uvicorn; the CLI query service remains available.") from error
    origins = _origins(allowed_origins)
    reader = HttpReadService(settings, limits)
    app = FastAPI(title="Agent Forge public metadata", docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.state.reader = reader
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"],
                           allow_headers=["Accept", "If-None-Match"], allow_credentials=False,
                           expose_headers=["ETag", "X-Request-ID", "Retry-After"], max_age=600)

    if event_log is not None:
        app.add_middleware(RequestLogMiddleware, events=event_log)

    def render(result):
        return Response(content=result.body, status_code=result.status, headers=result.headers)

    def respond(request: Request):
        if len(request.scope.get("query_string", b"")) > reader.limits.max_query_bytes:
            return render(reader.error_response("request_too_large", 414))
        path = request.scope["path"]
        root_path = request.scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        return render(reader.handle(request.method, path, request.query_params.multi_items(),
                                    if_none_match=request.headers.get("if-none-match")))

    # Fixed suffixes precede the catch-all ID route. The explicit /detail form handles suffix-like IDs.
    for suffix in ("detail", "versions", "metrics", "recommendations"):
        app.add_api_route("/api/v1/packages/{package_id:path}/" + suffix, respond, methods=["GET"], include_in_schema=False)
    for path in ("/health/live", "/health/ready", "/api/v1/packages", "/api/v1/changes", "/api/v1/facets", "/api/v1/recommendations", "/api/v1/recommendation-sources", "/api/v1/sources", "/api/v1/manifest", "/api/v1/dashboard",
                 "/api/v1/packages/{package_id:path}", "/api/v1/discoveries/{discovery_id:path}"):
        app.add_api_route(path, respond, methods=["GET"], include_in_schema=False)
    app.add_api_route("/{unmatched_path:path}", respond, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"], include_in_schema=False)

    @app.exception_handler(HTTPException)
    async def framework_error(request, error):
        code = "method_not_allowed" if error.status_code == 405 else "not_found" if error.status_code == 404 else "invalid_request"
        return render(reader.error_response(code, error.status_code if error.status_code in (400, 404, 405) else 400))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return render(reader.error_response("invalid_request", 400))

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        if event_log is not None:
            event_log.emit("server_error", error_kind=error_kind(error))
        return render(reader.error_response("internal_error", 500,
                      request_id=request.scope.get("state", {}).get("operational_request_id")))

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path.cwd(), help="Path used only to validate runtime separation; the API does not read canonical files")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--max-in-flight", type=int, default=4)
    parser.add_argument("--allow-origin", action="append", default=[])
    add_log_arguments(parser)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        with from_arguments("query", arguments, protected=(Path(__file__).resolve().parents[1], arguments.catalog, arguments.runtime)) as events:
            limits = HttpLimits(max_in_flight=arguments.max_in_flight)
            settings = Settings(arguments.catalog, arguments.runtime)
            app = create_app(settings, limits, allowed_origins=arguments.allow_origin, event_log=events)
            import uvicorn
            events.emit("service_start")
            try:
                with server_logging(events):
                    # One instance owns the admission budget and the log directory lease.
                    uvicorn.run(app, host=arguments.host, port=arguments.port, workers=1, proxy_headers=False,
                                log_config=None, access_log=False,
                                limit_concurrency=max(16, limits.max_in_flight * 4), backlog=64, timeout_keep_alive=5)
            finally:
                events.emit("service_stop")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        with OperationalLog("query") as events:
            events.emit("server_error", code="log_configuration_invalid", error_kind=error_kind(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
