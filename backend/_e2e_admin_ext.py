"""E2E for tenant-auth admin extensions: audit, invitations, transfer-kb, paging, fixed-role check."""
from __future__ import annotations
import sys, uuid, httpx

BASE = "http://localhost:8000"
c = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False)
SFX = uuid.uuid4().hex[:6]
P, F = 0, 0
fails = []

def ck(name, cond, detail=""):
    global P, F
    if cond: P += 1; print(f"  [PASS] {name}")
    else: F += 1; fails.append(f"{name}::{detail}"); print(f"  [FAIL] {name} -- {detail}")

def auth(t, extra=None):
    h = {"Authorization": f"Bearer {t}"}
    if extra: h.update(extra)
    return h

# super admin login (+ change pwd if needed)
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "ChangeMe!Admin2026"})
if r.status_code == 200 and r.json().get("must_change_password"):
    t = r.json()["access_token"]
    c.post("/api/auth/change-password", headers=auth(t),
           json={"old_password": "ChangeMe!Admin2026", "new_password": "SuperAdmin#New2026"})
r = c.post("/api/auth/login", json={"username": "superadmin", "password": "SuperAdmin#New2026"})
ck("super admin login", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
sa = r.json()["access_token"]

print("== admin role check ==")
# create tenant + admin
r = c.post("/api/admin/tenants", headers=auth(sa),
           json={"name": f"法院E-{SFX}", "admin_username": f"admine_{SFX}"})
ck("create tenant", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
tid = r.json()["id"]; atemp = r.json()["admin_temp_password"]
atok = c.post("/api/auth/login", json={"username": f"admine_{SFX}", "password": atemp, "tenant_id": tid}).json()["access_token"]
c.post("/api/auth/change-password", headers=auth(atok), json={"old_password": atemp, "new_password": "AdminE#2026"})
atok = c.post("/api/auth/login", json={"username": f"admine_{SFX}", "password": "AdminE#2026", "tenant_id": tid}).json()["access_token"]
# tenant admin identity（固定角色模型）：role == admin，非超管
me = c.get("/api/auth/me", headers=auth(atok)).json()
ck("tenant admin role == admin", me.get("role") == "admin", str(me))
ck("tenant admin not super admin", me.get("is_super_admin") is False, str(me))
# tenant admin cannot list tenants (platform) -> 403
ck("tenant admin tenant list -> 403", c.get("/api/admin/tenants", headers=auth(atok)).status_code == 403, "")

print("== user paging + search ==")
for i in range(3):
    c.post("/api/admin/users", headers=auth(atok), json={"username": f"alice{i}_{SFX}", "password": "Alice#2026"})
    c.post("/api/admin/users", headers=auth(atok), json={"username": f"bob{i}_{SFX}", "password": "Bobby#2026"})
r = c.get("/api/admin/users?page=1&page_size=2", headers=auth(atok))
ck("user list paginated", r.status_code == 200 and r.json()["page_size"] == 2 and len(r.json()["items"]) == 2, r.text[:120])
ck("user list total reflects all", r.json()["total"] >= 6, str(r.json().get("total")))
r = c.get(f"/api/admin/users?q=alice0_{SFX}", headers=auth(atok))
ck("user search by username", r.status_code == 200 and r.json()["total"] == 1, r.text[:120])

print("== transfer knowledge bases ==")
# create two users; user1 makes KBs; transfer to user2
u1 = c.post("/api/admin/users", headers=auth(atok), json={"username": f"owner1_{SFX}", "password": "Owner1#2026"}).json()
u2 = c.post("/api/admin/users", headers=auth(atok), json={"username": f"owner2_{SFX}", "password": "Owner2#2026"}).json()
u1tok = c.post("/api/auth/login", json={"username": f"owner1_{SFX}", "password": "Owner1#2026", "tenant_id": tid}).json()["access_token"]
kb_ids = []
for i in range(2):
    rk = c.post("/api/knowledge-bases", headers=auth(u1tok), json={"name": f"kb{i}_{SFX}"})
    kb_ids.append(rk.json()["id"])
# transfer
r = c.post(f"/api/admin/users/{u1['id']}/transfer-knowledge-bases", headers=auth(atok),
           json={"target_user_id": u2["id"]})
ck("transfer KB 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
ck("transfer count == 2", r.json().get("transferred_count") == 2, str(r.json()))
# user2 now sees them, user1 no longer owns (still tenant-public? no, private -> u1 can't read)
u2tok = c.post("/api/auth/login", json={"username": f"owner2_{SFX}", "password": "Owner2#2026", "tenant_id": tid}).json()["access_token"]
r = c.get(f"/api/knowledge-bases/{kb_ids[0]}", headers=auth(u2tok))
ck("target user can read transferred KB", r.status_code == 200, f"{r.status_code}")
r = c.get(f"/api/knowledge-bases/{kb_ids[0]}", headers=auth(u1tok))
ck("source user lost private KB (404)", r.status_code == 404, f"{r.status_code}")
# transfer to self -> 403; cross-tenant target -> 404
ck("transfer to self -> 403", c.post(f"/api/admin/users/{u2['id']}/transfer-knowledge-bases", headers=auth(atok), json={"target_user_id": u2["id"]}).status_code == 403, "")
ck("transfer to unknown user -> 404", c.post(f"/api/admin/users/{u2['id']}/transfer-knowledge-bases", headers=auth(atok), json={"target_user_id": "nonexistent"}).status_code == 404, "")

print("== invitations ==")
# super admin: create_tenant invite
r = c.post("/api/admin/invitations", headers=auth(sa),
           json={"scope": "create_tenant", "expires_in_hours": 24, "max_uses": 1})
ck("super create_tenant invite 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
tinv_token = r.json()["token"]
# tenant admin cannot create create_tenant invite -> 403
ck("tenant admin create_tenant invite -> 403",
   c.post("/api/admin/invitations", headers=auth(atok), json={"scope": "create_tenant", "expires_in_hours": 24}).status_code == 403, "")
# tenant admin create_user invite
r = c.post("/api/admin/invitations", headers=auth(atok),
           json={"scope": "create_user", "expires_in_hours": 24, "max_uses": 1})
ck("tenant admin create_user invite 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
uinv_token = r.json()["token"]
# info (no auth)
r = c.get(f"/api/invitations/{uinv_token}")
ck("invite info valid", r.status_code == 200 and r.json()["valid"] and r.json()["scope"] == "create_user", r.text[:120])
# accept create_user (no auth)
r = c.post(f"/api/invitations/{uinv_token}/accept", json={"username": f"invited_{SFX}", "password": "Invited#2026"})
ck("accept create_user 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
# invited user can login in that tenant
ck("invited user can login", c.post("/api/auth/login", json={"username": f"invited_{SFX}", "password": "Invited#2026", "tenant_id": tid}).status_code == 200, "")
# one-time used up -> second accept fails
r = c.post(f"/api/invitations/{uinv_token}/accept", json={"username": f"invited2_{SFX}", "password": "Invited#2026"})
ck("one-time invite exhausted -> 404", r.status_code == 404, f"{r.status_code}")
# accept create_tenant
r = c.post(f"/api/invitations/{tinv_token}/accept", json={"username": f"newadmin_{SFX}", "password": "NewAdmin#2026", "tenant_name": f"法院F-{SFX}"})
ck("accept create_tenant 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
new_tid = r.json().get("tenant_id")
# the new tenant admin can login and is admin (固定角色 role=admin)
ntok = c.post("/api/auth/login", json={"username": f"newadmin_{SFX}", "password": "NewAdmin#2026", "tenant_id": new_tid}).json()["access_token"]
nme = c.get("/api/auth/me", headers=auth(ntok)).json()
ck("invited tenant admin role == admin", nme.get("role") == "admin", str(nme))
ck("invited tenant admin not super admin", nme.get("is_super_admin") is False, str(nme))
# revoke invite
r = c.post("/api/admin/invitations", headers=auth(atok), json={"scope": "create_user", "expires_in_hours": 24})
rid = r.json()["id"]
ck("revoke invite 204", c.delete(f"/api/admin/invitations/{rid}", headers=auth(atok)).status_code == 204, "")

print("== audit logs ==")
# tenant admin sees own-tenant audit; should include user.create, user.transfer_kb, invitation.create
r = c.get("/api/admin/audit-logs?page=1&page_size=50", headers=auth(atok))
ck("audit list 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
actions = {x["action"] for x in r.json()["items"]}
ck("audit has user.create", "user.create" in actions, str(sorted(actions)))
ck("audit has user.transfer_kb", "user.transfer_kb" in actions, "")
ck("audit has invitation.create", "invitation.create" in actions, "")
# tenant admin audit is tenant-scoped: should NOT see super admin's tenant.create of OTHER tenants
# (their own tenant creation was done by super admin, so actor_tenant_id null -> not visible to tenant admin)
ck("tenant admin audit excludes platform tenant.create", "tenant.create" not in actions, str(sorted(actions)))
# super admin sees global incl tenant.create
r = c.get("/api/admin/audit-logs?page=1&page_size=50", headers=auth(sa))
sactions = {x["action"] for x in r.json()["items"]}
ck("super audit has tenant.create", "tenant.create" in sactions, str(sorted(sactions)))
# audit via api key channel forbidden (need a key first)
kr = c.post("/api/api-keys", headers=auth(atok), json={"name": "k", "scope": {"all_public_kbs": True, "explicit_kb_ids": []}})
keytok = kr.json()["key"]
ck("api key cannot read audit -> 403", c.get("/api/admin/audit-logs", headers=auth(keytok)).status_code == 403, "")

print("\n" + "=" * 50)
print(f"RESULT: {P} passed, {F} failed")
for x in fails: print("  - " + x)
sys.exit(1 if F else 0)
