import pipeline
from integrations import gcp


def run():
    logs = gcp.fetch_recent_errors()
    print(f"Fetched {len(logs)} error/critical log(s)")

    for log in logs:
        result = pipeline.process_log(log)

        if result["status"] == "duplicate":
            print(
                f"[dup]  matched row {result['matched_id']} "
                f"(distance={result['distance']:.4f}, now seen {result['occurrence_count']}x) "
                f"— reused stored solution, no Claude call"
            )
        else:
            print(
                f"[new]  row {result['id']} inserted "
                f"(file={result['source_file']})"
            )
            solution = result["solution"]
            print(f"       solution: {solution[:200]}{'...' if len(solution) > 200 else ''}")


if __name__ == "__main__":
    run()