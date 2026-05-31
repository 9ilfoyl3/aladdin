"""Verify the new admin endpoints that back the management UI pages."""
from __future__ import annotations
import sys, uuid, httpx

BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)
SFX = uuid.uuid4().hex[:6]
P, F = 0, 0
fails = []

def ck(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  [PASS] {name}")
    else:
        F += 1; fails.append(f"{name} :: {detail}"); print(f"  [FAIL] {name} -- {detail}")

def auth(t): return {"Authorization": f"Bearer {t}"}

# super admin: login + change pwd if needed
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "ChangeMe!Admin2026"})
if r.status_code == 200 and r.json().get("must_change_password"):
    tok = r.json()["access_token"]
    c.post("/api/auth/change-password", headers=auth(tok),
           json={"old_password": "ChangeMe!Admin2026", "new_password": "SuperAdmin#New2026"})
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "SuperAdmin#New2026"})
ck("super admin login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
sa = r.json()["access_token"]

# create tenant (Tenants page)
r = c.post("/api/admin/tenants", headers=auth(sa),
           json={"name": f"法院X-{SFX}", "admin_username": f"adminx_{SFX}"})
ck("create tenant", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
tid = r.json()["id"]; admin_temp = r.json()["admin_temp_password"]

# list tenants (Tenants page)
r = c.get("/api/admin/tenants", headers=auth(sa))
ck("list tenants", r.status_code == 200 and any(t["id"] == tid for t in r.json()), r.text[:120])

# tenant admin login + change pwd
r = c.post("/api/auth/login", json={"username": f"adminx_{SFX}", "password": admin_temp, "tenant_id": tid})
atok = r.json()["access_token"]
c.post("/api/auth/change-password", headers=auth(atok),
       json={"old_password": admin_temp, "new_password": "AdminX#Pass2026"})
r = c.post("/api/auth/login", json={"username": f"adminx_{SFX}", "password": "AdminX#Pass2026", "tenant_id": tid})
atok = r.json()["access_token"]

# list permission dict (Roles page)
r = c.get("/api/admin/permissions", headers=auth(atok))
ck("permission dict", r.status_code == 200 and len(r.json()) >= 20, f"{r.status_code} n={len(r.json()) if r.status_code==200 else '-'}")
ck("perm dict typed", r.status_code == 200 and all("code" in p and "type" in p for p in r.json()), "")

# list roles (Roles page) - builtin admin/user present
r = c.get("/api/admin/roles", headers=auth(atok))
ck("list roles", r.status_code == 200, r.text[:120])
role_names = {x["name"] for x in r.json()} if r.status_code == 200 else set()
ck("builtin roles present", {"admin", "user"} <= role_names, str(role_names))

# create custom role (Roles page)
r = c.post("/api/admin/roles", headers=auth(atok),
           json={"name": f"readonly_{SFX}", "permission_codes": ["kb:read", "menu:knowledge"]})
ck("create role", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
rid = r.json()["id"]

# update role permissions (Roles page edit)
r = c.put(f"/api/admin/roles/{rid}/permissions", headers=auth(atok),
          json={"permission_codes": ["kb:read", "kb:create", "menu:knowledge"]})
ck("update role perms", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

# create user (Users page)
r = c.post("/api/admin/users", headers=auth(atok),
           json={"username": f"ux_{SFX}", "role_names": ["user"]})
ck("create user", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
uid = r.json()["id"]; ck("user temp pwd returned", bool(r.json().get("temp_password")), "")

# list users (Users page) — now paginated PageResult
r = c.get("/api/admin/users", headers=auth(atok))
ck("list users", r.status_code == 200 and any(u["id"] == uid for u in r.json()["items"]), r.text[:120])

# get user roles (Users page role dialog)
r = c.get(f"/api/admin/users/{uid}/roles", headers=auth(atok))
ck("get user roles", r.status_code == 200 and "role_ids" in r.json(), r.text[:120])

# set user roles (assign custom role)
r = c.put(f"/api/admin/users/{uid}/roles", headers=auth(atok), json={"role_ids": [rid]})
ck("set user roles", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

# reset password (Users page)
r = c.post(f"/api/admin/users/{uid}/reset-password", headers=auth(atok))
ck("reset password", r.status_code == 200 and bool(r.json().get("temp_password")), r.text[:120])

# toggle user status (Users page)
r = c.put(f"/api/admin/users/{uid}/status", headers=auth(atok), json={"is_active": False})
ck("disable user", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
c.put(f"/api/admin/users/{uid}/status", headers=auth(atok), json={"is_active": True})

# delete custom role must reassign user first; reassign to builtin user then delete
r = c.get("/api/admin/roles", headers=auth(atok))
user_role_id = next(x["id"] for x in r.json() if x["name"] == "user")
c.put(f"/api/admin/users/{uid}/roles", headers=auth(atok), json={"role_ids": [user_role_id]})
r = c.delete(f"/api/admin/roles/{rid}", headers=auth(atok))
ck("delete custom role", r.status_code == 204, f"{r.status_code} {r.text[:120]}")

# builtin role cannot be deleted
r = c.delete(f"/api/admin/roles/{user_role_id}", headers=auth(atok))
ck("builtin role delete blocked -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

# tenant admin cannot access platform tenant mgmt is allowed? tenant:manage is platform-only.
r = c.get("/api/admin/tenants", headers=auth(atok))
ck("tenant admin cannot list tenants -> 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")

print("\n" + "=" * 50)
print(f"RESULT: {P} passed, {F} failed")
for x in fails: print("  - " + x)
sys.exit(1 if F else 0)
