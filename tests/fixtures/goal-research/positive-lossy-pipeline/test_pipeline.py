from pipeline import Event, encode_event, mean_pool, score


def main() -> None:
    events = [
        Event("wallet-a", 1, "buy", 2.0, 10.0),
        Event("wallet-b", 2, "sell", 4.0, 12.0),
    ]
    assert len(encode_event(events[0])) == 3
    assert mean_pool([encode_event(item) for item in events]) == (0.0, 3.0, 11.0)
    assert isinstance(score(events), float)
    print("ordinary positive fixture tests passed")


if __name__ == "__main__":
    main()
