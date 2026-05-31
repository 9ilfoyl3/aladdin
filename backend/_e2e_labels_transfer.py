"""验证：权限字典返回中文 label/type_label；转移知识库要求源用户先停用。"""
import uuid
import httpx

BASE = "http://localhost:8000"


def auth(t): return {"Authorization": f"Bearer {t}"}


def super_login(c):
    for pw in ("SuperAdminPwd2026", "ChangeMe!Admin2026"):
        r = c.post(f"{BASE}/api/auth/login", json={"username": "superadmin", "password": pw})
        if r.status_code == 200:
            d = r.json()
            if d.get("must_change_password"):
                c.post(f"{BASE}/api/auth/change-password", headers=auth(d["access_token"]),
                       json={"old_password": pw, "new_password": "SuperAdminPwd2026"})
                r = c.post(f"{BASE}/api/auth/login", json={"username": "superadmin", "password": "SuperAdminPwd2026"})
            return r.json()["access_token"]
    raise RuntimeError("超管登录失败")


def main():
    ok = []
    def check(n, c, e=""): ok.append((n, c)); print(f"[{'PASS' if c else 'FAIL'}] {n}" + (f" -- {e}" if e else ""))

    with httpx.Client(trust_env=False, timeout=30) as c:
        sa = super_login(c)
        sfx = uuid.uuid4().hex[:6]
        # 建租户 + 租管，改好口令拿到租管 token
        t = c.post(f"{BASE}/api/admin/tenants", headers=auth(sa),
                   json={"name": f"L_{sfx}", "admin_username": f"adm_{sfx}"}).json()
        atemp = t["admin_temp_password"]
        d = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": atemp}).json()
        c.post(f"{BASE}/api/auth/change-password", headers=auth(d["access_token"]),
               json={"old_password": atemp, "new_password": "AdmPwd123"})
        adm = c.post(f"{BASE}/api/auth/login", json={"username": f"adm_{sfx}", "password": "AdmPwd123"}).json()["access_token"]

        # 权限字典含中文 label/type_label
        pd = c.get(f"{BASE}/api/admin/permissions", headers=auth(adm)).json()
        check("权限字典非空", len(pd) > 0, f"count={len(pd)}")
        sample = next((p for p in pd if p["code"] == "menu:knowledge"), None)
        check("menu:knowledge 有中文 label", bool(sample) and sample.get("label") == "菜单：知识库", str(sample))
        check("含 type_label 中文", all(p.get("type_label") in ("功能权限", "菜单可见", "按钮可见") for p in pd))

        # 建两个用户用于转移
        u1 = c.post(f"{BASE}/api/admin/users", headers=auth(adm), json={"username": f"src_{sfx}", "role_names": []}).json()
        u2 = c.post(f"{BASE}/api/admin/users", headers=auth(adm), json={"username": f"dst_{sfx}", "role_names": []}).json()
        src_id, dst_id = u1["id"], u2["id"]

        # 源用户在用（启用）时转移应被拒
        r = c.post(f"{BASE}/api/admin/users/{src_id}/transfer-knowledge-bases", headers=auth(adm),
                   json={"target_user_id": dst_id})
        check("启用用户转移被拒(403)", r.status_code == 403, f"status={r.status_code} body={r.text[:120]}")

        # 停用源用户后允许转移
        c.put(f"{BASE}/api/admin/users/{src_id}/status", headers=auth(adm), json={"is_active": False})
        r = c.post(f"{BASE}/api/admin/users/{src_id}/transfer-knowledge-bases", headers=auth(adm),
                   json={"target_user_id": dst_id})
        check("停用后转移成功(200)", r.status_code == 200, f"status={r.status_code} body={r.text[:120]}")

    passed = sum(1 for _, c in ok if c)
    print(f"\n==== {passed}/{len(ok)} passed ====")
    raise SystemExit(0 if passed == len(ok) else 1)


if __name__ == "__main__":
    main()
