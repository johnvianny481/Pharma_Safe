"""PharmaSafe API: a modular monolith.

Modules (identity, tenancy, cases, intake, review, admin, reporting, audit) are
subpackages. A module never imports another module's internals or touches its tables;
it goes through that module's public interface or through outbox events.
"""
