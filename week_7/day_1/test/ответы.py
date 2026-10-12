"""Печатает из журнала сценария вопрос, вызванные инструменты, найденные источники и ответ."""
import json, re, sys
for строка in open(sys.argv[1]):
    з = json.loads(строка)
    вызовы = [в["function"]["name"] for m in з["messages"] if m.get("tool_calls") for в in m["tool_calls"]]
    вызовы = list(dict.fromkeys(вызовы))
    первое = next((m["content"] for m in з["messages"] if m["role"] == "user"), "")
    источники = list(dict.fromkeys(re.findall(r"^\[\d+\] (\S+)", первое, re.M)))
    print(f"## {з['query']}\n")
    print(f"- модель: `{з['model']}`, время: {з['elapsed_ms'] / 1000:.1f} с, токенов: {з['usage'].get('total_tokens')}")
    print(f"- найдено поиском: {', '.join(источники) or 'ничего'}")
    print(f"- вызваны инструменты: {', '.join(вызовы) or 'нет'}\n")
    print((з.get("response") or з.get("error") or "").strip() + "\n")
