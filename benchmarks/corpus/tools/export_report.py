import csv
import sys


def export_inspection_report(rows, path):
    """Write inspection rows to a spreadsheet-friendly csv file."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["LotNo", "PartNo", "Station", "DefectCode"])
        writer.writerows(rows)


if __name__ == "__main__":
    export_inspection_report([], sys.argv[1])
