CREATE RANGE INDEX `file_key` FOR (n:`File`) ON (n.`key`);
CREATE RANGE INDEX `file_path` FOR (n:`File`) ON (n.`path`);
CREATE RANGE INDEX `kept_key` FOR (n:`Kept`) ON (n.`key`);
CREATE RANGE INDEX `kept_path` FOR (n:`Kept`) ON (n.`path`);
CREATE RANGE INDEX `process_key` FOR (n:`Process`) ON (n.`key`);
