import requests

from app.util import helper
from .models import User


def make() -> User:
    return User(helper())
