import type {ColumnDataSource} from "@bokehjs/models/sources/column_data_source"
import {isNumber} from "@bokehjs/core/util/types"

export function transform_cds_to_records(
  cds: ColumnDataSource,
  addId: boolean = false,
  start: number = 0,
  internal_row_id_field: string = "__panel_row_id__",
): any {
  const data: any = []
  const columns = cds.columns()
  const cdsLength = cds.get_length()
  if (columns.length === 0 || cdsLength === null) {
    return []
  }
  const has_row_id_column = columns.includes(internal_row_id_field)

  for (let i = start; i < cdsLength; i++) {
    const item: any = {}
    for (const column of columns) {
      if (column === internal_row_id_field) {
        // Internal row-id transport field — skip it so it never leaks into
        // Tabulator row data. User columns that happen to have any other
        // name (including literally "__row_id__" or "__panel_row_id__")
        // are preserved because Python would have chosen a DIFFERENT,
        // non-colliding dynamic field name for the internal transport.
        continue
      }
      const array: any = cds.get_array(column)
      const shape = (array[0] == null || array[0].shape == null) ? null : array[0].shape
      if (shape != null && shape.length > 1 && isNumber(shape[0])) {
        item[column] = array.slice(i*shape[1], i*shape[1]+shape[1])
      } else if (array.length != cdsLength && (array.length % cdsLength === 0)) {
        const offset = array.length / cdsLength
        item[column] = array.slice(i*offset, i*offset+offset)
      } else {
        item[column] = array[i]
      }
    }
    if (addId) {
      // Use the dynamic internal row-id field name sent from Python via
      // the model's internal_row_id_field property. Falls back to the
      // positional index if the field is missing (backward compatibility).
      if (has_row_id_column) {
        const row_id_array = cds.get_array(internal_row_id_field)
        item._index = row_id_array[i]
      } else {
        item._index = i
      }
    }
    data.push(item)
  }
  return data
}

export function dict_to_records(data: any, index: boolean=true): any[] {
  const records: any[] = []
  for (let i = 0; i < data.index.length; i++) {
    const record: any = {}
    for (const col of data) {
      if (index || col !== "index") {
        record[col] = data[col][i]
      }
    }
  }
  return records
}
