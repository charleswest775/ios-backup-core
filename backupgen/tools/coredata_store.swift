// Create an empty SQLite store from a compiled Core Data model (.mom), the way an app's
// NSPersistentContainer would on first launch, so its table definitions can be harvested
// when no real backup holds the database.
//
// How it was used (iOS 26.5 Voice Memos, November 2026):
//
//   swiftc -O backupgen/tools/coredata_store.swift -o /tmp/coredata_store
//   MOMD="/Library/Developer/CoreSimulator/Volumes/iOS_23F77/Library/Developer/CoreSimulator/Profiles/Runtimes/iOS 26.5.simruntime/Contents/Resources/RuntimeRoot/System/Library/PrivateFrameworks/VoiceMemos.framework/VoiceMemos.momd"
//   /tmp/coredata_store "$MOMD/VoiceMemos14.mom" /tmp/CloudRecordings.db
//   python -m backupgen harvest --sqlite /tmp/CloudRecordings.db --database CloudRecordings.db \
//       --ios 26.5 --build 23F77 \
//       --source "Apple VoiceMemos.framework model VoiceMemos14, iOS 26.5 Simulator runtime (23F77); store created by Core Data, definitions only"
//
// VoiceMemos14 is the current version in that .momd (VersionInfo.plist). The store is
// opened with persistent history tracking on (Voice Memos syncs, so a device's store has
// Core Data's ACHANGE/ATRANSACTION tables, as the iOS 13 backups show) and in WAL mode,
// then checkpointed so the main file holds everything: the harvester opens databases
// read-only and immutable, which ignores a -wal file.
//
// What this can't reproduce: tables Core Data adds only on a device, such as CloudKit
// mirroring's ANSCK* metadata tables, if Voice Memos uses NSPersistentCloudKitContainer.
// Nothing is stored in the model's tables, so the harvested shape statistics only cover
// Core Data's own bookkeeping tables.

import CoreData
import Foundation
import SQLite3

let arguments = CommandLine.arguments
guard arguments.count == 3 else {
    FileHandle.standardError.write("usage: coredata_store <model.mom> <output.sqlite>\n".data(using: .utf8)!)
    exit(2)
}
let modelURL = URL(fileURLWithPath: arguments[1])
let storeURL = URL(fileURLWithPath: arguments[2])

guard let model = NSManagedObjectModel(contentsOf: modelURL) else {
    FileHandle.standardError.write("can't load the model at \(modelURL.path)\n".data(using: .utf8)!)
    exit(1)
}
for suffix in ["", "-wal", "-shm"] {
    try? FileManager.default.removeItem(atPath: storeURL.path + suffix)
}

let coordinator = NSPersistentStoreCoordinator(managedObjectModel: model)
let options: [String: Any] = [
    NSPersistentHistoryTrackingKey: true as NSNumber,
    NSSQLitePragmasOption: ["journal_mode": "WAL"],
]
do {
    let store = try coordinator.addPersistentStore(ofType: NSSQLiteStoreType, configurationName: nil,
                                                   at: storeURL, options: options)
    let context = NSManagedObjectContext(concurrencyType: .privateQueueConcurrencyType)
    context.persistentStoreCoordinator = coordinator
    var saveError: Error?
    context.performAndWait {
        do { try context.save() } catch { saveError = error }
    }
    if let saveError { throw saveError }
    try coordinator.remove(store)
} catch {
    FileHandle.standardError.write("Core Data couldn't create the store: \(error)\n".data(using: .utf8)!)
    exit(1)
}

// Fold the WAL into the main file; the header keeps saying WAL, as a device's copy does.
var db: OpaquePointer?
guard sqlite3_open(storeURL.path, &db) == SQLITE_OK else {
    FileHandle.standardError.write("can't reopen \(storeURL.path)\n".data(using: .utf8)!)
    exit(1)
}
if sqlite3_exec(db, "PRAGMA wal_checkpoint(TRUNCATE)", nil, nil, nil) != SQLITE_OK {
    FileHandle.standardError.write("checkpoint failed: \(String(cString: sqlite3_errmsg(db)))\n".data(using: .utf8)!)
    exit(1)
}
sqlite3_close(db)
for suffix in ["-wal", "-shm"] {
    try? FileManager.default.removeItem(atPath: storeURL.path + suffix)
}
print(storeURL.path)
