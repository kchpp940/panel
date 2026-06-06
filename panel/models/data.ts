import type {ColumnDataSource} from "@bokehjs/models/sources/column_data_source"
import {isNumber} from "@bokehjs/core/util/types"

const INTERNAL_ROW_ID_FIELD = "__panel_row_id__"

export function transform_cds_to_records(cds: ColumnDataSource, addId: boolean = false, start: number = 0): any {
  const data: any = []
  const columns = cds.columns()
  const cdsLength = cds.get_length()
  if (columns.length === 0 || cdsLength === null) {
    return []
  }
  const has_row_id_column = columns.includes(INTERNAL_ROW_ID_FIELD)

  for (let i = start; i < cdsLength; i++) {
    const item: any = {}
    for (const column of columns) {
      if (column === INTERNAL_ROW_ID_FIELD) {
        // Internal row-id transport field — store the value separately so
        // we can use it for row mapping but never expose it as a user-
        // visible data field on the Tabulator row.
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
      // Prefer the explicit internal row-id field sent from Python
      // (stable integer row id), fall back to positional index.
      // A user column literally named "__panel_row_id__" would have
      // collided in Python and raised an error, so we're safe here.
      const row_id_array = cds.get_array(INTERNAL_ROW_ID_FIELD)
      item._index = has_row_id_column ? row_id_array[i] : i
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
