import Foundation
import SQLite3

final class DatabaseService {
    private var db: OpaquePointer?

    init() {
        guard let dbPath = Bundle.main.path(forResource: "baseball_stats", ofType: "db") else {
            fatalError("baseball_stats.db not found in app bundle")
        }
        if sqlite3_open_v2(dbPath, &db, SQLITE_OPEN_READONLY, nil) != SQLITE_OK {
            let error = String(cString: sqlite3_errmsg(db))
            fatalError("Failed to open database: \(error)")
        }
    }

    deinit {
        sqlite3_close(db)
    }

    struct QueryResult {
        let columns: [String]
        let rows: [[String]]
    }

    func execute(sql: String) throws -> QueryResult {
        var statement: OpaquePointer?

        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            let error = String(cString: sqlite3_errmsg(db))
            throw DatabaseError.prepareFailed(error)
        }
        defer { sqlite3_finalize(statement) }

        let columnCount = sqlite3_column_count(statement)
        var columns: [String] = []
        for i in 0..<columnCount {
            let name = String(cString: sqlite3_column_name(statement, i))
            columns.append(name)
        }

        var rows: [[String]] = []
        while sqlite3_step(statement) == SQLITE_ROW {
            var row: [String] = []
            for i in 0..<columnCount {
                if let text = sqlite3_column_text(statement, i) {
                    row.append(String(cString: text))
                } else {
                    row.append("NULL")
                }
            }
            rows.append(row)
            if rows.count >= 50 { break }
        }

        return QueryResult(columns: columns, rows: rows)
    }

    enum DatabaseError: LocalizedError {
        case prepareFailed(String)

        var errorDescription: String? {
            switch self {
            case .prepareFailed(let msg): return "SQL error: \(msg)"
            }
        }
    }
}
