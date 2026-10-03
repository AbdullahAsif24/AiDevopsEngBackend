-- Multi-user deployment system database schema
-- Run this in your Supabase SQL editor to create the required tables

-- Enable UUID extension if not already enabled
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- User credentials for deployment platforms (Vercel, Render)
CREATE TABLE user_credentials (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('vercel', 'render')),
  access_token TEXT NOT NULL,
  refresh_token TEXT,
  token_expires_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  UNIQUE(user_id, platform)
);

-- Persistent job storage with user association
CREATE TABLE jobs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  job_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN (
    'queued', 'cloning', 'analyzing', 'generating', 
    'building', 'healing', 'deploying', 'done', 'failed', 'needs_review'
  )),
  repo_url TEXT NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  result JSONB,
  error TEXT,
  detection JSONB,
  deployment JSONB,
  logs JSONB DEFAULT '[]'::jsonb,
  repo_path TEXT
);

-- Environment variables per job
CREATE TABLE environment_variables (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  UNIQUE(job_id, key)
);

-- Create indexes for performance
CREATE INDEX idx_user_credentials_user_id ON user_credentials(user_id);
CREATE INDEX idx_user_credentials_platform ON user_credentials(platform);
CREATE INDEX idx_jobs_user_id ON jobs(user_id);
CREATE INDEX idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_env_vars_job_id ON environment_variables(job_id);

-- TEMPORARILY DISABLE RLS FOR TESTING
-- Comment these out to enable RLS again later
-- ALTER TABLE user_credentials ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE environment_variables ENABLE ROW LEVEL SECURITY;

-- Disable RLS temporarily to test
ALTER TABLE user_credentials DISABLE ROW LEVEL SECURITY;
ALTER TABLE jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE environment_variables DISABLE ROW LEVEL SECURITY;

-- RLS policies for user_credentials
-- Users can only read/write their own credentials
CREATE POLICY "Users can view own credentials" 
ON user_credentials FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own credentials" 
ON user_credentials FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own credentials" 
ON user_credentials FOR UPDATE 
USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own credentials" 
ON user_credentials FOR DELETE 
USING (auth.uid() = user_id);

-- RLS policies for jobs
-- Users can only read/write their own jobs
CREATE POLICY "Users can view own jobs" 
ON jobs FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own jobs" 
ON jobs FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own jobs" 
ON jobs FOR UPDATE 
USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own jobs" 
ON jobs FOR DELETE 
USING (auth.uid() = user_id);

-- RLS policies for environment_variables
-- Users can manage env vars for their own jobs
CREATE POLICY "Users can view env vars for own jobs" 
ON environment_variables FOR SELECT 
USING (
  EXISTS (
    SELECT 1 FROM jobs 
    WHERE jobs.id = environment_variables.job_id 
    AND jobs.user_id = auth.uid()
  )
);

CREATE POLICY "Users can insert env vars for own jobs" 
ON environment_variables FOR INSERT 
WITH CHECK (
  EXISTS (
    SELECT 1 FROM jobs 
    WHERE jobs.id = environment_variables.job_id 
    AND jobs.user_id = auth.uid()
  )
);

CREATE POLICY "Users can update env vars for own jobs" 
ON environment_variables FOR UPDATE 
USING (
  EXISTS (
    SELECT 1 FROM jobs 
    WHERE jobs.id = environment_variables.job_id 
    AND jobs.user_id = auth.uid()
  )
);

CREATE POLICY "Users can delete env vars for own jobs" 
ON environment_variables FOR DELETE 
USING (
  EXISTS (
    SELECT 1 FROM jobs 
    WHERE jobs.id = environment_variables.job_id 
    AND jobs.user_id = auth.uid()
  )
);

-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ language 'plpgsql';

-- Triggers to automatically update updated_at
CREATE TRIGGER update_user_credentials_updated_at 
BEFORE UPDATE ON user_credentials 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_jobs_updated_at 
BEFORE UPDATE ON jobs 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Grant necessary permissions
-- These will be handled by Supabase's automatic permissions, 
-- but we ensure the service role can access everything
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA public TO service_role;

-- =========================================================================
-- Auto-deploy on Push Tables
-- =========================================================================
CREATE TABLE IF NOT EXISTS auto_deploy_configs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  is_active BOOLEAN NOT NULL DEFAULT true,
  webhook_secret TEXT NOT NULL,
  webhook_url TEXT NOT NULL,
  auto_rollback BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_events (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  config_id UUID REFERENCES auto_deploy_configs(id) ON DELETE SET NULL,
  user_id UUID REFERENCES auth.users NOT NULL,
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  commit_message TEXT NOT NULL,
  committer TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('triggered', 'skipped', 'failed')),
  job_id TEXT,
  error_message TEXT,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auto_deploy_user_id ON auto_deploy_configs(user_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_user_id ON webhook_events(user_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_created_at ON webhook_events(created_at DESC);

-- =========================================================================
-- Secrets Vault Tables (Military-Grade Encrypted Storage)
-- =========================================================================
CREATE TABLE IF NOT EXISTS secrets (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  key TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('production', 'staging', 'development')),
  encrypted_value TEXT NOT NULL,
  masked_value TEXT NOT NULL,
  version INT NOT NULL DEFAULT 1,
  rotation_interval_days INT DEFAULT 30,
  last_rotated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  expires_at TIMESTAMP WITH TIME ZONE,
  shared_roles JSONB DEFAULT '["Admin", "Developer"]'::jsonb,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  UNIQUE(user_id, key, environment)
);

CREATE TABLE IF NOT EXISTS secret_audit_logs (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  secret_id UUID,
  secret_key TEXT NOT NULL,
  environment TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('create', 'reveal', 'rotate', 'update', 'delete', 'share')),
  actor TEXT NOT NULL,
  ip_address TEXT DEFAULT '127.0.0.1',
  details TEXT NOT NULL,
  timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_secrets_user_id ON secrets(user_id);
CREATE INDEX IF NOT EXISTS idx_secrets_env ON secrets(environment);
CREATE INDEX IF NOT EXISTS idx_secret_audit_user_id ON secret_audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_secret_audit_timestamp ON secret_audit_logs(timestamp DESC);

-- =========================================================================
-- Custom Domains & Health Check Tables
-- =========================================================================
CREATE TABLE IF NOT EXISTS custom_domains (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users NOT NULL,
  domain TEXT NOT NULL,
  job_id TEXT,
  target_url TEXT,
  status TEXT NOT NULL DEFAULT 'pending_dns',
  dns_type TEXT NOT NULL DEFAULT 'CNAME',
  dns_host TEXT NOT NULL DEFAULT '@',
  dns_target TEXT NOT NULL DEFAULT 'cname.aidevops.app',
  dns_ttl INT DEFAULT 60,
  dns_verified BOOLEAN DEFAULT false,
  ssl_status TEXT NOT NULL DEFAULT 'pending',
  ssl_issuer TEXT DEFAULT 'Let''s Encrypt Authority X3',
  ssl_expires_at TIMESTAMP WITH TIME ZONE,
  auto_ssl_renew BOOLEAN DEFAULT true,
  health_status TEXT NOT NULL DEFAULT 'pending',
  latency_ms INT,
  uptime_percent REAL DEFAULT 100.0,
  http_status_code INT,
  last_checked_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  UNIQUE(user_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_domains_user_id ON custom_domains(user_id);
CREATE INDEX IF NOT EXISTS idx_domains_domain ON custom_domains(domain);

-- Automatically update timestamps for new tables
CREATE TRIGGER update_auto_deploy_configs_updated_at 
BEFORE UPDATE ON auto_deploy_configs 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_secrets_updated_at 
BEFORE UPDATE ON secrets 
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();