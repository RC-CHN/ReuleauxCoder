"""The wire-affecting model parameters shared by chat and configuration probes."""


def model_request_parameters(settings, messages: list[dict], *, max_output_tokens=None) -> dict:
    params = {
        "model": settings.model, "messages": messages, "stream": True,
        "temperature": settings.temperature,
        "max_tokens": max(1, int(max_output_tokens)) if max_output_tokens is not None else settings.max_tokens,
    }
    if settings.reasoning_effort:
        mapping = settings.reasoning_effort_values or {"low": "low", "medium": "medium", "high": "high"}
        parameter = "reasoning_effort" if settings.request_mode == "responses" else settings.reasoning_effort_param
        params[parameter] = mapping.get(settings.reasoning_effort, settings.reasoning_effort)
    elif settings.thinking_enabled is not None:
        params["extra_body"] = {"thinking": {"type": "enabled" if settings.thinking_enabled else "disabled"}}
    return params
