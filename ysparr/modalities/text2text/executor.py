from ysparr.core.types import ExecutionResult, PromptRequest


def execute(request: PromptRequest, backend, storage) -> ExecutionResult:
    # minimal implementation to satisfy integration test

    output_path = storage.initialize(request)

    for chunk in backend.stream(request):
        storage.append(request, chunk)

    storage.finalize(request)

    return ExecutionResult(
        prompt_id=request.prompt_id,
        status="completed",
        output_path=output_path,
    )
