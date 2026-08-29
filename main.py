import pipeline
from integrations import gcp, notify


def run():
    logs = gcp.fetch_recent_errors()
    print(f"Fetched {len(logs)} error/critical log(s)")

    for log in logs:
        result = pipeline.process_log(log)
        notify.notify(result, log)

        if result["status"] == "duplicate":
            print(
                f"[dup]  matched row {result['matched_id']} "
                f"(distance={result['distance']:.4f}, now seen {result['occurrence_count']}x) "
                f"— reused stored solution, no chat model call"
            )
        elif result["status"] == "adapted":
            print(
                f"[adp]  row {result['id']} inserted, adapted from row {result['reference_id']} "
                f"(distance={result['distance']:.4f}, file={result['source_file']}) "
                f"— cheap adapt-model call"
            )
            solution = result["solution"]
            print(f"       solution: {solution[:200]}{'...' if len(solution) > 200 else ''}")
        else:
            print(
                f"[new]  row {result['id']} inserted "
                f"(file={result['source_file']})"
            )
            solution = result["solution"]
            print(f"       solution: {solution[:200]}{'...' if len(solution) > 200 else ''}")


if __name__ == "__main__":
    run()