const {createServer} = require("@lhci/server");

async function main() {
  const port = Number(process.env.PORT || 9001);
  const storage = {
    storageMethod: "sql",
    sqlDialect: process.env.LHCI_STORAGE__SQL_DIALECT || "sqlite",
    sqlDatabasePath: process.env.LHCI_STORAGE__SQL_DATABASE_PATH || "/data/lhci.db"
  };

  const basicAuth = process.env.LHCI_BASIC_AUTH__USERNAME
    ? {
        username: process.env.LHCI_BASIC_AUTH__USERNAME,
        password: process.env.LHCI_BASIC_AUTH__PASSWORD || ""
      }
    : undefined;

  const options = {port, storage};
  if (basicAuth) {
    options.basicAuth = basicAuth;
  }

  const server = await createServer(options);
  console.log(`LHCI server listening on ${server.port}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
