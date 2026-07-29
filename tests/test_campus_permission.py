from __future__ import annotations

from types import SimpleNamespace

from app.permissions.campus import CampusPermissionChecker


def _checker(monkeypatch) -> CampusPermissionChecker:
    monkeypatch.setattr(
        "app.permissions.campus.get_settings",
        lambda: SimpleNamespace(campus_admin_roles="campus_admin,school_admin"),
    )
    return CampusPermissionChecker()


def test_public_document_is_available_without_identity(monkeypatch):
    checker = _checker(monkeypatch)

    assert checker.can_access_document({"access_level": "public"}, None)


def test_department_document_requires_matching_department(monkeypatch):
    checker = _checker(monkeypatch)
    document = {"access_level": "department", "owner_dept": "computer_science"}

    assert checker.can_access_document(
        document,
        {"is_authenticated": True, "departments": ["computer_science"]},
    )
    assert not checker.can_access_document(
        document,
        {"is_authenticated": True, "departments": ["physics"]},
    )


def test_role_document_requires_matching_role(monkeypatch):
    checker = _checker(monkeypatch)
    document = {"access_level": "role", "owner_role": "graduate"}

    assert checker.can_access_document(
        document,
        {"is_authenticated": True, "roles": ["graduate"]},
    )
    assert not checker.can_access_document(document, {"is_authenticated": True, "roles": ["undergraduate"]})


def test_unknown_access_level_is_denied_and_admin_bypasses(monkeypatch):
    checker = _checker(monkeypatch)
    document = {"access_level": "unclassified"}

    assert not checker.can_access_document(document, {"is_authenticated": True})
    assert checker.can_access_document(document, {"is_authenticated": True, "roles": ["campus_admin"]})
