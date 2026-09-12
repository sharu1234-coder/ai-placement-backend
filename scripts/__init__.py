"""Operational scripts: backup, restore, and the alert runner a scheduler invokes.

A package rather than loose files so tests can import them (`from scripts import
run_due_alerts`) and assert on their behaviour. An operational script that nothing can test is
one that gets discovered to be broken by a silent night of undelivered alerts.
"""
