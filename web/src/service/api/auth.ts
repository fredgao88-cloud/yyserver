import { hospitalRequest } from './hospital';

interface BackendLoginResponse {
  access_token: string;
  token_type: string;
  expires_in_minutes: number;
  operator_id: string;
  display_name: string;
  role_type: string;
}

interface BackendMeResponse {
  operator_id: string;
  display_name: string;
  role_type: string;
  dept_scope: string;
  doctor_id: string;
  can_export_case: boolean;
}

export async function fetchLogin(userName: string, password: string): Promise<{ data: Api.Auth.LoginToken | null; error: Error | null }> {
  try {
    const res = await hospitalRequest<BackendLoginResponse>(
      '/api/auth/login',
      {
        method: 'POST',
        body: JSON.stringify({ operator_id: userName.trim(), password })
      },
      { requireAuth: false, silent401Logout: true }
    );
    return {
      data: {
        token: res.access_token,
        refreshToken: ''
      },
      error: null
    };
  } catch (err) {
    return { data: null, error: err as Error };
  }
}

export async function fetchGetUserInfo(): Promise<{ data: Api.Auth.UserInfo | null; error: Error | null }> {
  try {
    const res = await hospitalRequest<BackendMeResponse>('/api/auth/me', {}, { requireAuth: true, silent401Logout: true });
    return {
      data: {
        userId: res.operator_id,
        userName: res.display_name || res.operator_id,
        roles: [res.role_type],
        buttons: res.can_export_case ? ['export_case'] : [],
        deptScope: res.dept_scope,
        doctorId: res.doctor_id,
        canExportCase: res.can_export_case
      },
      error: null
    };
  } catch (err) {
    return { data: null, error: err as Error };
  }
}

export async function fetchRefreshToken(_refreshToken: string): Promise<{ data: Api.Auth.LoginToken; error: Error }> {
  return {
    data: { token: '', refreshToken: '' },
    error: new Error('不支持刷新令牌，请重新登录')
  };
}
