"""
quality_engine.py — Phase 48: Evaluation & Quality Scoring
==========================================================
Scores the workflow output for correctness, completeness, structure, and usability.
"""

def evaluate_quality(ctx_verification: dict, ctx_code: str, language: str) -> dict:
    score = 100
    penalties = []

    # 1. Correctness (Execution success)
    if not ctx_verification.get("ok"):
        score -= 40
        penalties.append("Execution failed (syntax/runtime errors).")
    elif ctx_verification.get("warning"):
        score -= 10
        penalties.append("Minor runtime issues detected.")

    # 2. Completeness (Code existence)
    if not ctx_code or len(ctx_code.strip()) < 10:
        score -= 50
        penalties.append("Code is empty or too short.")

    # 3. Structure & Usability
    if language in ["html", "javascript", "css"]:
        if "<html>" not in ctx_code.lower() and "<body" not in ctx_code.lower():
            score -= 20
            penalties.append("HTML structure incomplete (missing <html> or <body>).")

    # Limit boundaries
    score = max(0, min(score, 100))

    if score >= 90:
        verdict = "excellent"
    elif score >= 70:
        verdict = "good"
    elif score >= 50:
        verdict = "usable_with_fixes"
    else:
        verdict = "failed"

    return {
        "quality_score": score,
        "verdict": verdict,
        "penalties": penalties
    }
