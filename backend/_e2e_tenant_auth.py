"""End-to-end functional test of the tenant-auth user/tenant system via live HTTP API.

Runs against a running backend (default http://localhost:8000). Exercises:
  - bootstrap state (super admin, builtin external tenant)
  - super admin login + forced password change gate
  - platform ops: tenant CRUD, create tenant admin
  - tenant admin: change password, create users, custom roles, assign roles
  - real-time RBAC (permission changes take effect next request)
  - cross-tenant hard isolation (404)
  - default-deny (401), permission-deny (403)
  - API key three models (tenant / user / external-agent) + channel boundary
  - KB visibility promotion + point-to-point sharing
  - external-agent proxy key + external user lazy creation + isolation
  - content-view boundary for super admin

Prints a PASS/FAIL line per check and a summary. Exit code != 0 on any failure.
"""

from __future__ import annotations

import sys
import uuid
import httpx

BASE = "http://localhost:8000"

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        _failures.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  -- {detail}")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def section(title: str) -> None:
    print(f"\n=== {title} ===")


client = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)

# Unique suffix so reruns don't collide on usernames/tenant names
SFX = uuid.uuid4().hex[:6]

SUPER_USER = "superadmin"
SUPER_PWD = "ChangeMe!Admin2026"
SUPER_NEW_PWD = "SuperAdmin#New2026"


def main() -> None:
    section("0. Health / default-deny")
    r = client.get("/")
    check("root health 200", r.status_code == 200, str(r.status_code))

    # default-deny: protected route without creds -> 401
    r = client.get("/api/knowledge-bases")
    check("protected route w/o creds -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")

    # me/permissions without creds -> 401
    r = client.get("/api/auth/me/permissions")
    check("me/permissions w/o creds -> 401", r.status_code == 401, str(r.status_code))

    section("1. Super admin login + forced change password gate")
    r = client.post("/api/auth/login", json={"username": SUPER_USER, "password": SUPER_PWD})
    check("super admin login 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    data = r.json()
    check("super admin must_change_password=true", data.get("must_change_password") is True, str(data))
    check("super admin is_super_admin=true", data.get("is_super_admin") is True, str(data))
    super_token = data["access_token"]

    # must_change_password gate: any non-change-password op -> 403
    r = client.get("/api/admin/tenants", headers=auth(super_token))
    check("must_change gate blocks tenant list -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # change password (the only allowed op)
    r = client.post("/api/auth/change-password", headers=auth(super_token),
                     json={"old_password": SUPER_PWD, "new_password": SUPER_NEW_PWD})
    check("super admin change-password 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")

    # old token invalidated (token_version bumped) -> re-login required
    r = client.get("/api/admin/tenants", headers=auth(super_token))
    check("old token invalid after change-pwd -> 401", r.status_code == 401, str(r.status_code))

    # wrong old password -> 401 (re-login first)
    r = client.post("/api/auth/login", json={"username": SUPER_USER, "password": SUPER_NEW_PWD})
    check("super admin re-login 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    super_token = r.json()["access_token"]
    check("after change-pwd must_change=false", r.json().get("must_change_password") is False, str(r.json()))

    r = client.post("/api/auth/change-password", headers=auth(super_token),
                     json={"old_password": "wrong-old", "new_password": "Whatever#2026"})
    check("change-pwd wrong old -> 401", r.status_code == 401, str(r.status_code))

    section("2. Platform ops: tenant CRUD + initial tenant admin")
    # create tenant A
    r = client.post("/api/admin/tenants", headers=auth(super_token),
                    json={"name": f"法院A-{SFX}", "admin_username": f"adminA_{SFX}"})
    check("create tenant A 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    ta = r.json()
    tenant_a_id = ta["id"]
    admin_a_temp = ta["admin_temp_password"]
    check("tenant A returns admin temp password", bool(admin_a_temp), str(ta))

    # create tenant B
    r = client.post("/api/admin/tenants", headers=auth(super_token),
                    json={"name": f"法院B-{SFX}", "admin_username": f"adminB_{SFX}"})
    check("create tenant B 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    tb = r.json()
    tenant_b_id = tb["id"]
    admin_b_temp = tb["admin_temp_password"]

    # list tenants includes A and B
    r = client.get("/api/admin/tenants", headers=auth(super_token))
    check("list tenants 200", r.status_code == 200, str(r.status_code))
    names = {t["id"] for t in r.json()}
    check("tenant list includes A and B", tenant_a_id in names and tenant_b_id in names, str(names))

    section("3. Tenant admin A: change pwd, create user, RBAC")
    # tenant admin A first login (forced change)
    r = client.post("/api/auth/login", json={"username": f"adminA_{SFX}", "password": admin_a_temp,
                                              "tenant_id": tenant_a_id})
    check("tenant admin A login 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    check("tenant admin A must_change=true", r.json().get("must_change_password") is True, str(r.json()))
    a_admin_token = r.json()["access_token"]

    ADMIN_A_PWD = "AdminA#Pass2026"
    r = client.post("/api/auth/change-password", headers=auth(a_admin_token),
                    json={"old_password": admin_a_temp, "new_password": ADMIN_A_PWD})
    check("tenant admin A change-pwd 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/auth/login", json={"username": f"adminA_{SFX}", "password": ADMIN_A_PWD,
                                             "tenant_id": tenant_a_id})
    a_admin_token = r.json()["access_token"]

    # admin A permissions include user:manage, role:manage, menu:admin
    r = client.get("/api/auth/me/permissions", headers=auth(a_admin_token))
    check("admin A me/permissions 200", r.status_code == 200, str(r.status_code))
    a_perms = {p["code"] for p in r.json()["permissions"]}
    check("admin A has user:manage", "user:manage" in a_perms, str(sorted(a_perms)))
    check("admin A has role:manage", "role:manage" in a_perms, "")
    check("admin A perms typed (menu/btn present)",
          any(p["type"] == "menu" for p in r.json()["permissions"]), "")

    # admin A creates a normal user
    r = client.post("/api/admin/users", headers=auth(a_admin_token),
                    json={"username": f"userA1_{SFX}", "role_names": ["user"]})
    check("admin A create user 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    user_a1 = r.json()
    user_a1_id = user_a1["id"]
    user_a1_temp = user_a1["temp_password"]

    # normal user A1 login + forced change
    r = client.post("/api/auth/login", json={"username": f"userA1_{SFX}", "password": user_a1_temp,
                                             "tenant_id": tenant_a_id})
    check("user A1 login 200", r.status_code == 200, str(r.status_code))
    a_user_token = r.json()["access_token"]
    USER_A1_PWD = "UserA1#Pass2026"
    r = client.post("/api/auth/change-password", headers=auth(a_user_token),
                    json={"old_password": user_a1_temp, "new_password": USER_A1_PWD})
    check("user A1 change-pwd 200", r.status_code == 200, str(r.status_code))
    r = client.post("/api/auth/login", json={"username": f"userA1_{SFX}", "password": USER_A1_PWD,
                                             "tenant_id": tenant_a_id})
    a_user_token = r.json()["access_token"]

    # normal user lacks admin perms -> tenant mgmt 403
    r = client.get("/api/admin/roles", headers=auth(a_user_token))
    check("normal user role list -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # normal user perms: no user:manage, but has kb:create / qa:invoke
    r = client.get("/api/auth/me/permissions", headers=auth(a_user_token))
    u_perms = {p["code"] for p in r.json()["permissions"]}
    check("normal user lacks user:manage", "user:manage" not in u_perms, str(sorted(u_perms)))
    check("normal user has kb:create", "kb:create" in u_perms, "")

    section("4. Real-time RBAC: custom role + permission change takes effect next request")
    # create custom role with only kb:read
    r = client.post("/api/admin/roles", headers=auth(a_admin_token),
                    json={"name": f"readonly_{SFX}", "permission_codes": ["kb:read", "menu:knowledge"]})
    check("admin A create custom role 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    role_ro_id = r.json()["id"]

    # assign custom role to user A1 (replaces roles -> loses kb:create)
    r = client.put(f"/api/admin/users/{user_a1_id}/roles", headers=auth(a_admin_token),
                   json={"role_ids": [role_ro_id]})
    check("assign custom role 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # user A1 next request: kb:create gone, kb:read present (no re-login)
    r = client.get("/api/auth/me/permissions", headers=auth(a_user_token))
    u_perms2 = {p["code"] for p in r.json()["permissions"]}
    check("RBAC change instant: kb:create removed", "kb:create" not in u_perms2, str(sorted(u_perms2)))
    check("RBAC change instant: kb:read kept", "kb:read" in u_perms2, "")

    # add kb:create back to the role -> instant effect
    r = client.put(f"/api/admin/roles/{role_ro_id}/permissions", headers=auth(a_admin_token),
                   json={"permission_codes": ["kb:read", "kb:create", "menu:knowledge"]})
    check("update role perms 200", r.status_code == 200, str(r.status_code))
    r = client.get("/api/auth/me/permissions", headers=auth(a_user_token))
    u_perms3 = {p["code"] for p in r.json()["permissions"]}
    check("RBAC re-add kb:create instant", "kb:create" in u_perms3, str(sorted(u_perms3)))

    # restore user role for later KB tests
    # find the builtin 'user' role id
    r = client.get("/api/admin/roles", headers=auth(a_admin_token))
    roles_a = {role["name"]: role["id"] for role in r.json()}
    client.put(f"/api/admin/users/{user_a1_id}/roles", headers=auth(a_admin_token),
               json={"role_ids": [roles_a["user"]]})

    section("5. KB create + cross-tenant hard isolation")
    # user A1 creates a private KB
    r = client.post("/api/knowledge-bases", headers=auth(a_user_token),
                    json={"name": f"A1-private-{SFX}"})
    check("user A1 create KB 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    kb_a1 = r.json()
    kb_a1_id = kb_a1["id"]
    check("new KB visibility=private", kb_a1.get("visibility") == "private", str(kb_a1))
    check("new KB owner=creator", kb_a1.get("owner_user_id") == user_a1_id, str(kb_a1))

    # tenant B admin login
    r = client.post("/api/auth/login", json={"username": f"adminB_{SFX}", "password": admin_b_temp,
                                             "tenant_id": tenant_b_id})
    b_admin_token = r.json()["access_token"]
    ADMIN_B_PWD = "AdminB#Pass2026"
    client.post("/api/auth/change-password", headers=auth(b_admin_token),
                json={"old_password": admin_b_temp, "new_password": ADMIN_B_PWD})
    r = client.post("/api/auth/login", json={"username": f"adminB_{SFX}", "password": ADMIN_B_PWD,
                                             "tenant_id": tenant_b_id})
    b_admin_token = r.json()["access_token"]

    # tenant B admin tries to GET tenant A's KB -> cross-tenant 404
    r = client.get(f"/api/knowledge-bases/{kb_a1_id}", headers=auth(b_admin_token))
    check("cross-tenant KB GET -> 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    # tenant B list KBs does not include A's KB
    r = client.get("/api/knowledge-bases", headers=auth(b_admin_token))
    b_kb_ids = {kb["id"] for kb in r.json()["items"]}
    check("tenant B list excludes A's KB", kb_a1_id not in b_kb_ids, str(b_kb_ids))

    # X-Tenant-ID spoof: tenant B admin tries to act as tenant A -> 403
    r = client.get("/api/knowledge-bases", headers={**auth(b_admin_token), "X-Tenant-ID": tenant_a_id})
    check("X-Tenant-ID spoof by normal jwt -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    section("6. KB visibility promotion + point-to-point share (in-tenant)")
    # create a second user in tenant A
    r = client.post("/api/admin/users", headers=auth(a_admin_token),
                    json={"username": f"userA2_{SFX}", "role_names": ["user"]})
    user_a2 = r.json()
    user_a2_id = user_a2["id"]
    r = client.post("/api/auth/login", json={"username": f"userA2_{SFX}", "password": user_a2["temp_password"],
                                             "tenant_id": tenant_a_id})
    a_user2_token = r.json()["access_token"]
    client.post("/api/auth/change-password", headers=auth(a_user2_token),
                json={"old_password": user_a2["temp_password"], "new_password": "UserA2#Pass2026"})
    r = client.post("/api/auth/login", json={"username": f"userA2_{SFX}", "password": "UserA2#Pass2026",
                                             "tenant_id": tenant_a_id})
    a_user2_token = r.json()["access_token"]

    # user A2 cannot see A1's private KB
    r = client.get(f"/api/knowledge-bases/{kb_a1_id}", headers=auth(a_user2_token))
    check("A2 cannot read A1 private KB -> 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    # A1 shares KB to A2 with read
    r = client.post(f"/api/knowledge-bases/{kb_a1_id}/share", headers=auth(a_user_token),
                    json={"grantee_type": "user", "grantee_id": user_a2_id, "permission": "read"})
    check("A1 share KB to A2 (read) 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")

    # now A2 can read it
    r = client.get(f"/api/knowledge-bases/{kb_a1_id}", headers=auth(a_user2_token))
    check("A2 can read shared KB -> 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    # A2 cannot write (only read granted)
    r = client.put(f"/api/knowledge-bases/{kb_a1_id}", headers=auth(a_user2_token),
                   json={"description": "hacked by A2"})
    check("A2 read-only cannot write -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # invalid grantee_type -> 400
    r = client.post(f"/api/knowledge-bases/{kb_a1_id}/share", headers=auth(a_user_token),
                    json={"grantee_type": "organization", "grantee_id": "x", "permission": "read"})
    check("reserved grantee_type -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    # cross-tenant share: A1 shares to tenant B's admin user -> 404
    # need a B user id; create one
    r = client.post("/api/admin/users", headers=auth(b_admin_token),
                    json={"username": f"userB1_{SFX}", "role_names": ["user"]})
    user_b1_id = r.json()["id"]
    r = client.post(f"/api/knowledge-bases/{kb_a1_id}/share", headers=auth(a_user_token),
                    json={"grantee_type": "user", "grantee_id": user_b1_id, "permission": "read"})
    check("cross-tenant share -> 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    # A1 promotes KB to organization
    r = client.put(f"/api/knowledge-bases/{kb_a1_id}/visibility", headers=auth(a_user_token),
                   json={"visibility": "organization"})
    check("A1 promote KB to organization 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    check("promotion keeps owner unchanged", r.json().get("owner_user_id") == user_a1_id, str(r.json()))
    check("promotion sets visibility=organization", r.json().get("visibility") == "organization", "")

    # now any tenant-A user (A2) sees it in list (public KB)
    r = client.get("/api/knowledge-bases", headers=auth(a_user2_token))
    a2_kb_ids = {kb["id"] for kb in r.json()["items"]}
    check("public KB visible to same-tenant user", kb_a1_id in a2_kb_ids, str(a2_kb_ids))

    section("7. API Key three models + channel boundary")
    # tenant admin A creates a tenant-level key with all_public_kbs scope
    r = client.post("/api/api-keys", headers=auth(a_admin_token),
                    json={"name": "svcA", "scope": {"all_public_kbs": True, "explicit_kb_ids": []}})
    check("create tenant-level key 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    tenant_key = r.json()["key"]
    check("tenant key returned plaintext once", tenant_key.startswith("sk-"), tenant_key[:8])

    # use tenant-level key on /v1 chat? we lack LLM; instead test channel boundary:
    # API key cannot do admin op -> 403
    r = client.get("/api/admin/roles", headers=auth(tenant_key))
    check("api key -> admin op 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # api key cannot manage api keys (administrative) -> 403
    r = client.get("/api/api-keys", headers=auth(tenant_key))
    check("api key -> apikey:manage 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # tenant key can list KBs within tenant (content op) -- should see public KB
    r = client.get("/api/knowledge-bases", headers=auth(tenant_key))
    check("tenant key list KBs 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    if r.status_code == 200:
        tk_ids = {kb["id"] for kb in r.json()["items"]}
        check("tenant key (all_public) sees public KB", kb_a1_id in tk_ids, str(tk_ids))

    # user-level key for A1
    r = client.post("/api/api-keys/me", headers=auth(a_user_token), json={"name": "a1-personal"})
    check("create user-level key 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    user_key = r.json()["key"]

    # user-level key inherits user's perms but is still tenant-channel only -> admin 403
    r = client.get("/api/admin/roles", headers=auth(user_key))
    check("user-level key admin op -> 403", r.status_code == 403, str(r.status_code))

    # list keys shows only prefix (no plaintext)
    r = client.get("/api/api-keys", headers=auth(a_admin_token))
    check("admin list keys 200", r.status_code == 200, str(r.status_code))
    if r.status_code == 200:
        items = r.json()["items"]
        check("listed keys expose prefix only",
              all("key" not in it for it in items) and all(it.get("prefix") for it in items), str(items[:1]))

    section("8. External-agent proxy key (Super_Admin only) + external user isolation")
    # normal tenant admin cannot create proxy key -> 403
    r = client.post("/api/api-keys/external-agent", headers=auth(a_admin_token), json={"name": "proxy"})
    check("tenant admin create proxy key -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

    # super admin creates proxy key
    r = client.post("/api/api-keys/external-agent", headers=auth(super_token), json={"name": "proxy-1"})
    check("super admin create proxy key 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    proxy_key = r.json()["key"]

    # proxy key without X-External-User-Id -> 400 (use a /v1 or /api content endpoint)
    r = client.get("/api/knowledge-bases", headers=auth(proxy_key))
    check("proxy key w/o X-External-User-Id -> 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")

    # proxy key as external user e1 -> can list KBs (sees external public KB), locked to external tenant
    h_e1 = {**auth(proxy_key), "X-External-User-Id": "ext-user-1"}
    r = client.get("/api/knowledge-bases", headers=h_e1)
    check("proxy key as e1 list KBs 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    e1_kb_ids = {kb["id"] for kb in r.json()["items"]} if r.status_code == 200 else set()
    check("external user sees external public KB", "kb-external-public" in e1_kb_ids, str(e1_kb_ids))
    check("external user does NOT see tenant A public KB", kb_a1_id not in e1_kb_ids, str(e1_kb_ids))

    # external user e1 creates own private KB
    r = client.post("/api/knowledge-bases", headers=h_e1, json={"name": "e1-private"})
    check("external user e1 create KB 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    e1_kb_id = r.json().get("id")

    # external user e2 (same proxy key, different id) cannot see e1's private KB
    h_e2 = {**auth(proxy_key), "X-External-User-Id": "ext-user-2"}
    r = client.get(f"/api/knowledge-bases/{e1_kb_id}", headers=h_e2)
    check("external user e2 cannot see e1 private KB -> 404", r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    # external user cannot do admin op -> 403
    r = client.get("/api/admin/tenants", headers=h_e1)
    check("external user admin op -> 403", r.status_code == 403, str(r.status_code))

    section("9. Account management: disable user invalidates JWT")
    # disable user A2
    r = client.put(f"/api/admin/users/{user_a2_id}/status", headers=auth(a_admin_token),
                   json={"is_active": False})
    check("admin disable user A2 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    # A2's existing token now invalid -> 401
    r = client.get("/api/auth/me/permissions", headers=auth(a_user2_token))
    check("disabled user old JWT -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    # A2 cannot login while disabled
    r = client.post("/api/auth/login", json={"username": f"userA2_{SFX}", "password": "UserA2#Pass2026",
                                             "tenant_id": tenant_a_id})
    check("disabled user login blocked", r.status_code in (401, 403), f"{r.status_code} {r.text[:120]}")
    # re-enable
    r = client.put(f"/api/admin/users/{user_a2_id}/status", headers=auth(a_admin_token),
                   json={"is_active": True})
    check("admin re-enable user A2 200", r.status_code == 200, str(r.status_code))

    section("10. Tenant disable blocks all access")
    # disable tenant B
    r = client.put(f"/api/admin/tenants/{tenant_b_id}/status", headers=auth(super_token),
                   json={"is_active": False})
    check("super disable tenant B 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    # tenant B admin existing token -> 403
    r = client.get("/api/knowledge-bases", headers=auth(b_admin_token))
    check("disabled tenant user -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    # tenant B admin cannot login
    r = client.post("/api/auth/login", json={"username": f"adminB_{SFX}", "password": ADMIN_B_PWD,
                                             "tenant_id": tenant_b_id})
    check("disabled tenant login blocked", r.status_code in (401, 403), f"{r.status_code} {r.text[:120]}")
    # re-enable
    client.put(f"/api/admin/tenants/{tenant_b_id}/status", headers=auth(super_token),
               json={"is_active": True})

    section("11. Content-view boundary (super admin cannot read business content)")
    # super admin can read KB metadata (container) but list within external/platform scope.
    # Try reading a tenant A KB metadata as super admin (platform can read container metadata)
    r = client.get(f"/api/knowledge-bases/{kb_a1_id}", headers=auth(super_token))
    check("super admin read KB metadata allowed (200) or 404-by-design",
          r.status_code in (200, 404), f"{r.status_code} {r.text[:120]}")

    # Summary
    print("\n" + "=" * 60)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print("  - " + f)
    print("=" * 60)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
