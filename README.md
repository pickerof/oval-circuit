# Bing Shing

This is the local public-league candidate for Bing Shing, a Mech Royale entry for the Builders Buildathon. The bot routes around campus buildings, takes nearby loot and supply drops, leads shots against moving opponents, and seeks cover when it needs to recover.

The bot uses Python's standard library and the official `mechfight.py` transport wrapper from the [Builders starter kit](https://www.joinbuilders.org/buildathon/tracks/builders). The scorer supplies the arena files at runtime.

## Check the entry

Extract the official starter kit. Set `KIT` to the absolute path of its `mechfight-kit` directory, then run from this repository:

```sh
docker build --platform linux/amd64 -t oval-circuit .
python3 "$KIT/check/check.py" --map-dir /arena -- docker run --rm -i \
  --platform linux/amd64 --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "$KIT/arena:/arena:ro" oval-circuit
```

The checker verifies protocol compliance using recorded observations. Placement is measured in the official practice queue.
