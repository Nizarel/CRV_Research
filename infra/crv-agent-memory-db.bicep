// Agent-memory database for the CRV research backend, on the EXISTING PostgreSQL server.
//
// Deliberately database-only: it references the server as `existing`, so deploying it never
// changes server SKU, networking, Entra administrators, extensions or other databases
// (unlike redeploying postgres-ingestion-only.bicep). It is separate from the maintenance
// agent's `agent-memory` database and from `ingestion-catalog`.
// Tables are created by `crv-agent setup-memory` (migration identity) and the runtime login
// receives DML only through `crv-agent grant-memory-runtime`.
// The server must be running for the deployment to succeed.

@description('Existing PostgreSQL Flexible Server name')
param postgresServerName string

@description('Agent-memory database name')
param databaseName string = 'crv-agent-memory'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: postgresServerName
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

output databaseName string = database.name
