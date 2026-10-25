"""PharmaSafe domain: the E2B(R3)-aligned case model and deterministic rules.

This package must stay free of I/O, databases, web frameworks and AI calls. Pydantic is
the one dependency: it gives the case model strict validation and an exact JSON form,
which versioning and signatures hash. Everything here is pure, typed (mypy strict) and
covered to at least 85%, because it holds the logic that regulators care about: case
validity, seriousness, date and unit checks, and regulatory clocks.
"""

__version__ = "0.1.0"
