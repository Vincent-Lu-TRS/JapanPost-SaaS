import copy
import re


class FakeWorksheet:
    def __init__(self, rows, *, gid=465870894, row_count=None, formula_cells=None):
        self.id = gid
        self.title = "target"
        self.rows = [list(row) + [""] * (10 - len(row)) for row in rows]
        self.row_count = row_count if row_count is not None else max(len(self.rows), 1)
        self.calls = []
        self.fail_add_rows = None
        self.fail_batch_update = None
        self.drop_columns = set()
        self.drop_rows = set()
        self.stale_reads_remaining = 0
        self._stale_rows = None
        self.formula_cells = dict(formula_cells or {})

    def col_values(self, column):
        source = (
            self._stale_rows
            if self.stale_reads_remaining and self._stale_rows is not None
            else self.rows
        )
        values = [row[column - 1] for row in source]
        while values and values[-1] == "":
            values.pop()
        if (
            column == 10
            and self.stale_reads_remaining
            and self._stale_rows is not None
        ):
            self.stale_reads_remaining -= 1
        return values

    def add_rows(self, count):
        self.calls.append(("add_rows", count))
        if self.fail_add_rows:
            raise self.fail_add_rows
        self.row_count += count

    def get(self, a1_range, value_render_option=None):
        if a1_range != "B:J":
            raise ValueError(f"unsupported grid range: {a1_range}")
        rendered = []
        for row_number in range(1, len(self.rows) + 1):
            values = []
            for column_number in range(2, 11):
                if (
                    value_render_option == "FORMULA"
                    and (row_number, column_number) in self.formula_cells
                ):
                    values.append(self.formula_cells[(row_number, column_number)])
                else:
                    values.append(self.rows[row_number - 1][column_number - 1])
            rendered.append(values)
        return rendered

    def batch_update(self, batch, value_input_option=None):
        self.calls.append(("batch_update", batch, value_input_option))
        if self.fail_batch_update:
            raise self.fail_batch_update
        self._stale_rows = copy.deepcopy(self.rows)
        for update in batch:
            self._apply_a1_update(update["range"], update["values"])

    def _apply_a1_update(self, a1_range, values):
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", a1_range)
        if not match or match.group(2) != match.group(4):
            raise ValueError(f"unsupported range: {a1_range}")
        start_col = self._column_number(match.group(1))
        end_col = self._column_number(match.group(3))
        row_number = int(match.group(2))
        if row_number > self.row_count:
            raise ValueError("grid_limit")
        if row_number in self.drop_rows:
            return
        while len(self.rows) < row_number:
            self.rows.append([""] * 10)
        for offset, value in enumerate(values[0]):
            column_number = start_col + offset
            if column_number > end_col:
                raise ValueError("too_many_values")
            column_letter = self._column_letter(column_number)
            if column_letter not in self.drop_columns:
                self.rows[row_number - 1][column_number - 1] = value

    @staticmethod
    def _column_number(label):
        value = 0
        for character in label:
            value = value * 26 + ord(character) - ord("A") + 1
        return value

    @staticmethod
    def _column_letter(number):
        label = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            label = chr(ord("A") + remainder) + label
        return label


class FakeSpreadsheet:
    def __init__(self, worksheet):
        self.worksheet = worksheet

    def get_worksheet_by_id(self, gid):
        if gid != self.worksheet.id:
            raise KeyError(gid)
        return self.worksheet


class FakeClient:
    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.opened_keys = []

    def open_by_key(self, key):
        self.opened_keys.append(key)
        return FakeSpreadsheet(self.worksheet)
