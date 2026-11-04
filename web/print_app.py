# поток: print
"""Print-only ASGI app for the authenticated be.beyindi.com proxy."""
from fastapi import FastAPI

from web.print_api import make_router, production_service


def create_app(get_service=production_service):
    application = FastAPI(title='Печать заказов', docs_url=None,
                          redoc_url=None, openapi_url=None)
    application.include_router(make_router(get_service, standalone=True))
    return application


app = create_app()
