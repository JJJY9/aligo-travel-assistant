---
name: trip-budget
description: Use this skill when the user asks for a trip budget, cost estimate, or expense breakdown (transport / hotel / food / tickets). Returns a structured budget suggestion.
---

# Trip Budget Skill

根据行程与偏好给出分项预算建议。由 IntentionAgent 在预算 / 费用相关请求时调度。

## 输出

返回 JSON：`{"status": "success", "<result_key>": "建议正文", "answer": "建议正文"}`。
