"""_api_skills.py — Skill / Permission handlers."""
from __future__ import annotations
from singularity.scheduler import dispatcher as disp_mod

def skill_list():
    try:
        from skills.skill_loader import load_skills
        all_skills = load_skills()
        return {"skills": [{"name": s.name, "description": s.description, "type": s.type,
            "args": s.arguments, "source": s.source, "body": s.body[:200]} for s in all_skills.values()]}, 200
    except Exception as e: return {"error": str(e)}, 500

def skill_add(name, description="", skill_type="prompt", args=None, body=""):
    from skills.skill_loader import create_user_skill
    create_user_skill(name, description, skill_type, args or [], body)
    disp_mod.invalidate_skill_cache(); return {"ok": True, "name": name}, 200

def skill_delete(name):
    from skills.skill_loader import delete_user_skill
    ok = delete_user_skill(name); disp_mod.invalidate_skill_cache(); return {"ok": ok}, 200

def agent_skill_list(level, model):
    from skills.skill_loader import get_agent_skills, load_skills
    return {"skill_names": get_agent_skills(level, model), "available": list(load_skills().keys())}, 200

def agent_skill_update(level, model, skill_names):
    from skills.skill_loader import set_agent_skills
    set_agent_skills(level, model, skill_names); disp_mod.invalidate_skill_cache(level, model); return {"ok": True}, 200

def perm_profiles():
    from .permission import get_store; return {"profiles": get_store().list_profiles()}, 200
def perm_profiles_add(name, profile):
    from .permission import get_store; get_store().add_profile(name, profile); return {"ok": True}, 200
def perm_profiles_delete(name):
    from .permission import get_store; get_store().remove_profile(name); return {"ok": True}, 200
def perm_bind(level, model, profile):
    from .permission import get_store; get_store().bind_agent(level, model, profile); return {"ok": True}, 200
def perm_unbind(level, model):
    from .permission import get_store; get_store().unbind_agent(level, model); return {"ok": True}, 200
