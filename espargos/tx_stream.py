"""Feed stdin or a file to the stream configured in the controller web UI."""

import argparse
from contextlib import nullcontext
import sys
import struct

from .pool import Pool


def transmit(source, pool):
    """Preserve the prepared scene and stream bytes until EOF."""
    tx = pool.tx
    with tx.open_configured_stream() as stream:
        if stream.mode=="gfsk":
            return transmit_packets(source,tx,stream)
        stride = stream.stride
        minimum = 1024 if stream.mode == "cs8" else 4
        prefill = max(minimum, min(stream.capacity // 2, int(stream.sample_rate_hz * .1)))
        initial = source.read(prefill * stride)
        if len(initial) < minimum * stride:
            raise ValueError(f"Input needs at least {minimum} complete samples or indices")
        stream.write(initial)
        sent = len(initial) // stride
        tx.mute(False)
        stream.start()
        # read1 returns available bytes without waiting for a full block. It
        # batches files/pipes efficiently while keeping slow live sources
        # responsive. Retain a split IQ pair across arbitrary pipe reads.
        read = getattr(source, "read1", None)
        size = stream.chunk * stride if read is not None else max(1, min(stream.chunk, int(stream.sample_rate_hz * .02))) * stride
        read = read or source.read
        pending = b""
        while True:
            data = read(size)
            if not data:
                if pending:
                    raise ValueError("Input ends with an incomplete I/Q sample")
                break
            data = pending + data
            complete = len(data) // stride * stride
            if complete:
                stream.write(data[:complete])
                sent += complete // stride
            pending = data[complete:]
        stream.finish()
        timeout = max(10, (min(stream.offset, stream.capacity) + 8) / stream.sample_rate_hz + 2)
        stream.wait(timeout=timeout)
        states = stream.status()
        missing = stream.previous_missing + max(s["missing"] for s in states)
        clipped = max(s["clipped"] for s in states)
        print(f"Sent {sent} units; {missing} missing, {clipped} clipped; {stream.resumptions} resumptions.", file=sys.stderr)


def transmit_packets(source, tx, stream):
    """stdin: uint16 little-endian byte length, then one complete air packet."""

    def exact(n, allow_eof=False):
        out = bytearray()
        while len(out) < n:
            part = source.read(n - len(out))
            if not part:
                if allow_eof and not out:
                    return None
                raise ValueError("Input ends inside a GFSK packet")
            out.extend(part)
        return bytes(out)

    packets = 0
    while True:
        header = exact(2, True)
        if header is None:
            break
        size = struct.unpack("<H", header)[0]
        if size > 1020:
            raise ValueError("GFSK air packet exceeds 1020 bytes")
        stream.write_packet(exact(size), gap_us=stream.packet_gap_us)
        packets += 1
        if not stream.started and stream.offset >= stream.capacity // 2:
            tx.mute(False)
            stream.start()
    if not packets:
        raise ValueError("Input contains no GFSK packets")
    if not stream.started:
        tx.mute(False)
        stream.start()
    stream.finish()
    stream.wait(timeout=10)
    states = stream.status()
    print(
        f"Sent {packets} packets; {stream.previous_missing + max(s['missing'] for s in states)} missing symbol ticks.",
        file=sys.stderr,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="+", help="Controller addresses; prepare matching Stream beams and calibrate the arrays together first")
    parser.add_argument("--transport", choices=("udp", "websocket"), default="udp")
    parser.add_argument("--file", help="Input bytes; defaults to stdin (CS8 pairs, symbol indices, or length-prefixed GFSK packets)")
    args = parser.parse_args(argv)
    pool = Pool(args.host)
    try:
        pool.open(transports=[args.transport])
        with open(args.file, "rb") if args.file else nullcontext(sys.stdin.buffer) as source:
            transmit(source, pool)
        return 0
    except KeyboardInterrupt:
        print("Stream stopped.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Stream failed: {error}", file=sys.stderr)
        return 1
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
