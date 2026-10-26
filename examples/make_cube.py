"""Write calibration_cube.stl: a 20 mm cube, the usual first test print."""
import struct
from pathlib import Path

SIZE = 20.0


def cube_triangles(s: float):
    v = [(x, y, z) for z in (0, s) for y in (0, s) for x in (0, s)]
    # Each face as two triangles, counter-clockwise seen from outside.
    faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    normals = [(0, 0, -1), (0, 0, 1), (0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0)]
    for (a, b, c, d), n in zip(faces, normals, strict=True):
        yield n, (v[a], v[b], v[c])
        yield n, (v[a], v[c], v[d])


def main() -> None:
    tris = list(cube_triangles(SIZE))
    out = Path(__file__).with_name("calibration_cube.stl")
    with out.open("wb") as fh:
        fh.write(b"calibration cube 20mm".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for n, pts in tris:
            fh.write(struct.pack("<3f", *n))
            for p in pts:
                fh.write(struct.pack("<3f", *p))
            fh.write(b"\0\0")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
