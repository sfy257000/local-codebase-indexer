"""Shared BOM-aware decoding and binary detection for indexing and diagnostics."""
import codecs


def is_binary_data(data: bytes) -> bool:
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE,
                        codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    return b'\x00' in data[:8192]


def decode_bytes(data: bytes) -> str:
    # UTF-32 LE starts with the UTF-16 LE BOM, so the order matters.
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode('utf-32')
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode('utf-16')
    for encoding in ('utf-8-sig', 'big5', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def iter_decoded_lines(stream):
    """Stream BOM-tagged Unicode correctly; preserve legacy per-line fallbacks."""
    import io
    prefix = stream.read(4)
    stream.seek(0)
    encoding = ('utf-32' if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)) else
                'utf-16' if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else None)
    if encoding:
        wrapper = io.TextIOWrapper(stream, encoding=encoding, errors='replace')
        try:
            for line in wrapper:
                yield line.rstrip('\r\n')
        finally:
            wrapper.detach()
    else:
        for raw in stream:
            yield decode_bytes(raw.rstrip(b'\r\n'))
