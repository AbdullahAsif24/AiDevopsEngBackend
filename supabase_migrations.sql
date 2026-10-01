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